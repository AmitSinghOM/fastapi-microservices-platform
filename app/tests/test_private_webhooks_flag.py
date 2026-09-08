"""The development-only private-webhook escape hatch (quick-start receiver).

``allow_private_webhooks`` exists so a local clean-machine run can complete
a signed delivery against the example receiver. It must stay default-off in
every environment, be refused outside development at configuration load,
and never relax anything except the localhost/non-global host rules.
"""

import pytest

from app.config import Settings
from app.tests.test_delivery_contracts import worker_settings
from app.webhook_security import UnsafeWebhookUrl, validate_webhook_url


def test_default_is_off_and_deployed_environments_refuse_it():
    assert worker_settings().allow_private_webhooks is False
    base = dict(
        _env_file=None,
        secret_key="s" * 32,
        api_key_pepper="p" * 32,
        webhook_signing_key="w" * 32,
        worker_egress_proxy_url="http://egress-proxy.internal:3128",
    )
    for environment in ("test", "staging", "production"):
        with pytest.raises(ValueError, match="only in development"):
            Settings(
                environment=environment,
                allow_private_webhooks=True,
                **base,
            )
    development = Settings(
        environment="development", allow_private_webhooks=True, **base
    )
    assert development.allow_private_webhooks is True


@pytest.mark.asyncio
async def test_private_targets_stay_rejected_without_the_flag():
    for url in (
        "http://localhost:9000/webhooks",
        "http://127.0.0.1:9000/webhooks",
        "https://127.0.0.1/webhooks",
    ):
        with pytest.raises(UnsafeWebhookUrl):
            await validate_webhook_url(url, allow_http=True)


@pytest.mark.asyncio
async def test_flag_permits_loopback_but_not_other_rules():
    assert await validate_webhook_url(
        "http://127.0.0.1:9000/webhooks", allow_http=True, allow_private=True
    )
    assert await validate_webhook_url(
        "http://localhost:9000/webhooks", allow_http=True, allow_private=True
    )
    # Scheme, credential, and fragment rules are not relaxed.
    with pytest.raises(UnsafeWebhookUrl):
        await validate_webhook_url(
            "ftp://127.0.0.1/webhooks", allow_http=True, allow_private=True
        )
    with pytest.raises(UnsafeWebhookUrl):
        await validate_webhook_url(
            "http://user:pass@127.0.0.1:9000/x",
            allow_http=True,
            allow_private=True,
        )
    with pytest.raises(UnsafeWebhookUrl):
        await validate_webhook_url(
            "http://127.0.0.1:9000/x#frag",
            allow_http=True,
            allow_private=True,
        )
    # HTTPS-only environments still require port 443 semantics.
    with pytest.raises(UnsafeWebhookUrl):
        await validate_webhook_url(
            "https://127.0.0.1:8443/webhooks", allow_private=True
        )
