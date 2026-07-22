"""Run the outbound-only Telegram long-poll worker (no public listener)."""

from __future__ import annotations

import os
import signal
import time

from channel_gateway import AuthBindingClient, ChannelGateway, ProviderUnavailable
from unison_common.trust import read_secret_setting


def build_gateway() -> ChannelGateway:
    root_key = read_secret_setting("CHANNEL_GATEWAY_ROOT_KEY")
    workload_secret = read_secret_setting("AUTH_CHANNEL_WORKLOAD_SECRET")
    if not root_key or not workload_secret:
        raise RuntimeError("gateway root key and auth workload secret are required")
    authority = AuthBindingClient(
        os.getenv("AUTH_URL", "http://auth:8088"),
        os.getenv("AUTH_CHANNEL_WORKLOAD_ID", "unison-comms-channel-gateway"),
        workload_secret,
    )
    return ChannelGateway(os.getenv("CHANNEL_GATEWAY_DB", "/data/comms/channel-gateway.db"), root_key, authority)


def main() -> None:
    gateway = build_gateway()
    running = True

    def stop(*_args: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    backoff = 1
    while running:
        try:
            accounts = gateway.active_account_ids()
            for account_id in accounts:
                gateway.poll(account_id)
            backoff = 1
            if not accounts:
                time.sleep(5)
        except ProviderUnavailable:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
