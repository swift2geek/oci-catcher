import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from itertools import cycle

import oci

log = logging.getLogger("oci-catcher")

TRANSIENT_CODES = {"InternalError", "OutOfCapacity", "OutOfHostCapacity"}
FATAL_CODES = {"NotAuthorizedOrNotFound", "NotAuthenticated", "InvalidParameter", "LimitExceeded", "QuotaExceeded"}
ACTIVE_STATES = {"PROVISIONING", "STARTING", "RUNNING"}
RATE_LIMIT_BASE = 300
RATE_LIMIT_MAX = 1800
EXIT_FATAL = 3


@dataclass(frozen=True)
class Settings:
    compartment_id: str
    subnet_id: str
    image_id: str
    ssh_public_key: str
    shape: str = "VM.Standard.A1.Flex"
    ocpus: float = 2
    memory_gb: float = 12
    boot_volume_gb: int = 50
    display_name: str = "free-vps"
    availability_domains: tuple[str, ...] = ()
    retry_interval: int = 120
    heartbeat_hours: float = 6
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        ads = tuple(ad.strip() for ad in env.get("AVAILABILITY_DOMAINS", "").split(",") if ad.strip())
        return cls(
            compartment_id=env["OCI_COMPARTMENT_ID"],
            subnet_id=env["OCI_SUBNET_ID"],
            image_id=env["OCI_IMAGE_ID"],
            ssh_public_key=env["SSH_PUBLIC_KEY"].strip(),
            shape=env.get("OCI_SHAPE", cls.shape),
            ocpus=float(env.get("OCPUS", cls.ocpus)),
            memory_gb=float(env.get("MEMORY_GB", cls.memory_gb)),
            boot_volume_gb=int(env.get("BOOT_VOLUME_GB", cls.boot_volume_gb)),
            display_name=env.get("DISPLAY_NAME", cls.display_name),
            availability_domains=ads,
            retry_interval=int(env.get("RETRY_INTERVAL", cls.retry_interval)),
            heartbeat_hours=float(env.get("HEARTBEAT_HOURS", cls.heartbeat_hours)),
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        )


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage" if token and chat_id else None
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        log.info("notify: %s", text)
        if not self.url:
            return
        body = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=15).close()
        except OSError as error:
            log.warning("telegram send failed: %s", error)


@dataclass
class Catcher:
    settings: Settings
    notify: Telegram
    config: dict = field(init=False)
    compute: oci.core.ComputeClient = field(init=False)
    attempts: int = 0
    last_error: str = ""

    def __post_init__(self) -> None:
        self.config = oci.config.from_file()
        self.compute = oci.core.ComputeClient(self.config, retry_strategy=oci.retry.NoneRetryStrategy())

    def availability_domains(self) -> list[str]:
        if self.settings.availability_domains:
            return list(self.settings.availability_domains)
        identity = oci.identity.IdentityClient(self.config)
        return [ad.name for ad in identity.list_availability_domains(self.settings.compartment_id).data]

    def existing_instance(self) -> oci.core.models.Instance | None:
        instances = self.compute.list_instances(
            self.settings.compartment_id, display_name=self.settings.display_name
        ).data
        return next((i for i in instances if i.lifecycle_state in ACTIVE_STATES), None)

    def launch_details(self, availability_domain: str) -> oci.core.models.LaunchInstanceDetails:
        s = self.settings
        return oci.core.models.LaunchInstanceDetails(
            availability_domain=availability_domain,
            compartment_id=s.compartment_id,
            display_name=s.display_name,
            shape=s.shape,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=s.ocpus, memory_in_gbs=s.memory_gb),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=s.image_id, boot_volume_size_in_gbs=s.boot_volume_gb
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=s.subnet_id, assign_public_ip=True),
            metadata={"ssh_authorized_keys": s.ssh_public_key},
        )

    def public_ip(self, instance_id: str) -> str:
        attachments = self.compute.list_vnic_attachments(self.settings.compartment_id, instance_id=instance_id).data
        network = oci.core.VirtualNetworkClient(self.config)
        for attachment in attachments:
            ip = network.get_vnic(attachment.vnic_id).data.public_ip
            if ip:
                return ip
        return "<no public ip>"

    def report_success(self, instance: oci.core.models.Instance) -> None:
        log.info("instance %s launched in %s, waiting for RUNNING", instance.id, instance.availability_domain)
        oci.wait_until(
            self.compute, self.compute.get_instance(instance.id), "lifecycle_state", "RUNNING", max_wait_seconds=900
        )
        ip = self.public_ip(instance.id)
        self.notify.send(
            f"✅ {self.settings.display_name} RUNNING\n"
            f"{self.settings.shape} {self.settings.ocpus:g} OCPU / {self.settings.memory_gb:g} GB\n"
            f"AD: {instance.availability_domain}\n"
            f"ssh ubuntu@{ip}\n"
            f"attempts: {self.attempts}"
        )

    def hunt(self) -> None:
        ads = self.availability_domains()
        s = self.settings
        self.notify.send(
            f"🎯 hunting {s.shape} {s.ocpus:g}/{s.memory_gb:g}GB as '{s.display_name}'\n"
            f"ADs: {', '.join(ads)}\ninterval: {s.retry_interval}s"
        )
        rate_limit_delay = RATE_LIMIT_BASE
        next_heartbeat = time.monotonic() + s.heartbeat_hours * 3600

        for ad in cycle(ads):
            self.attempts += 1
            delay = s.retry_interval
            try:
                instance = self.compute.launch_instance(self.launch_details(ad)).data
                self.report_success(instance)
                return
            except oci.exceptions.ServiceError as error:
                self.last_error = f"{error.status} {error.code}: {error.message}"
                if error.status == 429:
                    delay, rate_limit_delay = rate_limit_delay, min(rate_limit_delay * 2, RATE_LIMIT_MAX)
                    log.warning("[%s] rate limited, backing off %ss", ad, delay)
                elif error.code in FATAL_CODES:
                    self.notify.send(f"🛑 fatal, stopping: {self.last_error}")
                    sys.exit(EXIT_FATAL)
                else:
                    rate_limit_delay = RATE_LIMIT_BASE
                    log.info("[%s] #%d %s", ad, self.attempts, self.last_error)
            except (oci.exceptions.RequestException, OSError) as error:
                self.last_error = f"network: {error}"
                log.warning("[%s] %s", ad, self.last_error)

            if time.monotonic() >= next_heartbeat:
                self.notify.send(f"⏳ still hunting: {self.attempts} attempts\nlast: {self.last_error}")
                next_heartbeat = time.monotonic() + s.heartbeat_hours * 3600
            time.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    notify = Telegram(settings.telegram_token, settings.telegram_chat_id)
    catcher = Catcher(settings, notify)

    existing = catcher.existing_instance()
    if existing:
        notify.send(f"ℹ️ '{settings.display_name}' already exists ({existing.lifecycle_state}), nothing to do")
        return
    catcher.hunt()


if __name__ == "__main__":
    main()
