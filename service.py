from dataclasses import dataclass, field
from typing import Any

from rich import print_json
from xcore.sdk import get_logger
from xcore.services.base import BaseService, ServiceStatus

from .providers import StripeProvider
from .providers.base import CheckoutResult, PaymentStatus, SubscriptionStatus

logger = get_logger("xpay")


@dataclass
class Product:
    title: str
    name: str
    amount: float
    currency: str = "usd"

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)

    def to_dict(self):

        form: dict = {}
        form[self.title] = {
            "name": self.name,
            "amount": self.amount,
            "currency": self.currency,
        }
        return form


@dataclass
class PayConfig:
    secret_key: str
    publishable_key: str
    webhook_secret: str = "change-me-in-production"
    domain: str = "http://localhost:8000"
    products: list[Product] = field(default_factory=list)

    def __scan(self):
        if self.webhook_secret == "change-me-in-production":
            logger.warning("change webhooks in production mode")

    @classmethod
    def from_dict(cls, d: dict):

        return cls(**d)

    def to_dicts(
        self,
    ):

        # parser
        self.__scan()
        products = [i.to_dict() for i in self.products]

        return {
            "secret_key": self.secret_key,
            "publishable_key": self.publishable_key,
            "webhook_secret": self.webhook_secret,
            "domain": self.domain,
            "products": products,
        }


class PayProvider(BaseService):
    name = "xpay"

    def __init__(self, config: dict) -> None:
        self._status = ServiceStatus.INITIALIZING

        self.provider = StripeProvider(config=config)

    async def init(self) -> None:
        await self.provider._init()
        self._status = ServiceStatus.READY
        return

    async def shutdown(self) -> None:
        await self.provider._shutdown()
        return

    async def health_check(self) -> tuple[bool, str]:
        return await self.provider._health_check()

    def status(self) -> dict[str, Any]:
        return self.provider._status()

    async def checkout(
        self,
        product_id: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:

        return await self.provider.create_checkout(
            product_id, success_path, cancel_path, metadata
        )

    async def verify(self, session_id: str) -> PaymentStatus:
        return await self.provider.verify_payment(session_id)

    async def list_catalog(self) -> list[dict[str, Any]]:
        """Catalogue Stripe (produits actifs + prix par défaut) pour le seed."""
        return await self.provider.list_catalog()

    async def subcribe(
        self,
        price_id: str,
        customer_email: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:
        return await self.provider.create_subscription_checkout(
            price_id, customer_email, success_path, cancel_path, metadata
        )

    async def unsubcribe(self, id: str, imediatly: bool = False) -> SubscriptionStatus:

        return await self.provider.cancel_subscription(id, imediatly)
