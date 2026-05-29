"""
StripeProvider — Implémentation Stripe de PaymentProvider pour xcore v2.

Déclaration dans xcore.yaml :
    services:
      extensions:
        stripe:
          module: myapp.payments.stripe_service:StripeProvider
          config:
            secret_key:       ${STRIPE_SECRET_KEY}
            publishable_key:  ${STRIPE_PUBLISHABLE_KEY}
            webhook_secret:   ${STRIPE_WEBHOOK_SECRET}
            domain:           https://myapp.com
            products:
              pro:
                name: "Plan Pro"
                amount: 2900
                currency: usd

Accès depuis un plugin Trusted :
    stripe = self.get_service("ext.stripe")

    result = await stripe.create_checkout("pro")
    status = await stripe.verify_payment(result.session_id)
    ok     = await stripe.get_subscription("user@ex.com")
    url    = await stripe.create_portal("user@ex.com")

    @stripe.on("payment.succeeded")
    async def on_paid(event: WebhookEvent): ...

    # Dans get_router() — route webhook FastAPI
    event = stripe.parse_webhook(body, request.headers["stripe-signature"])
    await stripe.handle_webhook(event)
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

import stripe as _stripe
from .config import  _normalize_event
from xcore.sdk import get_logger

from ..base import (
    CheckoutResult,
    PaymentProvider,
    PaymentStatus,
    SubscriptionStatus,
    WebhookEvent,
)

logger = get_logger("xpay")



# ---------------------------------------------------------------------------
# StripeProvider
# ---------------------------------------------------------------------------

class StripeProvider(PaymentProvider):
    """
    Implémentation Stripe de PaymentProvider.

    Conforme au contrat PaymentProvider — compatible avec ExtensionLoader xcore.
    """

    name = "stripe"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._webhook_secret: str = ""
        self._products: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, list[Callable]] = {}

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    async def _init(self) -> None:
        secret_key = self._config.get("secret_key")
        if not secret_key:
            raise ValueError(
                "[stripe] 'secret_key' manquant. "
                "Ajoutez config.secret_key dans xcore.yaml."
            )

        _stripe.api_key = secret_key
        self._webhook_secret = self._config.get("webhook_secret", "")
        self._products = self._config.get("products", {})

        # Vérification de la connexion API
        await _stripe.Account.retrieve_async()

        self._ready = True
        logger.info("[stripe] prêt (domain=%s, produits=%s)", self._domain(), list(self._products))

    async def _shutdown(self) -> None:
        self._ready = False
        self._handlers.clear()
        logger.info("[stripe] arrêté")

    async def _health_check(self) -> tuple[bool, str]:
        if not self._ready:
            return False, "non initialisé"
        try:
            await _stripe.Account.retrieve_async()
            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    def _status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": "ready" if self._ready else "uninitialized",
            "domain": self._domain(),
            "products": list(self._products.keys()),
            "webhook_handlers": {
                evt: len(handlers)
                for evt, handlers in self._handlers.items()
            },
        }

    # ------------------------------------------------------------------
    # Paiements one-time
    # ------------------------------------------------------------------

    async def create_checkout(
        self,
        product_id: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:
        self._require_ready()

        product = self._get_product(product_id)
        session = await _stripe.checkout.Session.create_async(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": product.get("currency", "usd"),
                    "product_data": {"name": product["name"]},
                    "unit_amount": int(product["amount"]),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{self._domain()}{success_path}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{self._domain()}{cancel_path}",
            metadata=metadata or {},
        )
        return CheckoutResult(
            url=session.url,
            session_id=session.id,
            provider=self.name,
            metadata={"product_id": product_id},
        )

    async def verify_payment(self, session_id: str) -> PaymentStatus:
        self._require_ready()

        session = await _stripe.checkout.Session.retrieve_async(session_id)
        paid = session.payment_status == "paid"
        return PaymentStatus(
            paid=paid,
            session_id=session_id,
            customer_email=session.customer_details.email if paid else None,
            amount=session.amount_total,
            currency=session.currency,
            metadata={"stripe_session": session.id},
        )

    # ------------------------------------------------------------------
    # Abonnements
    # ------------------------------------------------------------------

    async def create_subscription_checkout(
        self,
        price_id: str,
        customer_email: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:
        self._require_ready()

        customer = await self.get_or_create_customer(customer_email)
        session = await _stripe.checkout.Session.create_async(
            customer=customer["id"],
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{self._domain()}{success_path}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{self._domain()}{cancel_path}",
            metadata=metadata or {},
        )
        return CheckoutResult(
            url=session.url,
            session_id=session.id,
            provider=self.name,
            metadata={"price_id": price_id, "customer_email": customer_email},
        )

    async def get_subscription(self, customer_email: str) -> SubscriptionStatus:
        self._require_ready()

        customer = await self._find_customer(customer_email)
        if not customer:
            raise ValueError(f"[stripe] Aucun client pour : {customer_email}")

        subs = await _stripe.Subscription.list_async(
            customer=customer["id"],
            status="active",
            limit=1,
        )
        if not subs.data:
            raise ValueError(f"[stripe] Aucun abonnement actif pour : {customer_email}")

        sub = subs.data[0]
        return SubscriptionStatus(
            active=True,
            subscription_id=sub.id,
            customer_id=sub.customer,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            metadata={"stripe_status": sub.status},
        )

    async def cancel_subscription(
        self,
        subscription_id: str,
        immediately: bool = False,
    ) -> SubscriptionStatus:
        self._require_ready()

        if immediately:
            sub = await _stripe.Subscription.cancel_async(subscription_id)
        else:
            sub = await _stripe.Subscription.modify_async(
                subscription_id,
                cancel_at_period_end=True,
            )

        return SubscriptionStatus(
            active=sub.status == "active",
            subscription_id=sub.id,
            customer_id=sub.customer,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            metadata={"stripe_status": sub.status},
        )

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    async def get_or_create_customer(self, email: str) -> dict[str, Any]:
        self._require_ready()

        existing = await _stripe.Customer.list_async(email=email, limit=1)
        if existing.data:
            c = existing.data[0]
        else:
            c = await _stripe.Customer.create_async(email=email)

        return {"id": c.id, "email": c.email}

    # ------------------------------------------------------------------
    # Portail de facturation
    # ------------------------------------------------------------------

    async def create_portal(
        self,
        customer_email: str,
        return_path: str = "/dashboard",
    ) -> str:
        self._require_ready()

        customer = await self._find_customer(customer_email)
        if not customer:
            raise ValueError(f"[stripe] Aucun client pour : {customer_email}")

        portal = await _stripe.billing_portal.Session.create_async(
            customer=customer["id"],
            return_url=f"{self._domain()}{return_path}",
        )
        return portal.url

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def parse_webhook(self, payload: bytes, signature: str) -> WebhookEvent:
        if not self._webhook_secret:
            raise ValueError(
                "[stripe] 'webhook_secret' manquant. "
                "Ajoutez config.webhook_secret dans xcore.yaml."
            )
        raw = _stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature,
            secret=self._webhook_secret,
        )
        return _normalize_event(raw)

    async def handle_webhook(self, event: WebhookEvent) -> None:
        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            logger.debug("[stripe] webhook non géré : %s", event.event_type)
            return

        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("[stripe] erreur handler '%s'", event.event_type)

    def on(self, event_type: str) -> Callable:
        """
        Décorateur pour enregistrer un handler sur un type d'événement normalisé.

        Types disponibles : payment.succeeded, payment.failed, payment.expired,
        subscription.created, subscription.updated, subscription.cancelled,
        subscription.paused, subscription.resumed, invoice.upcoming, invoice.paid.

        Accepte aussi les types Stripe raw (ex: "checkout.session.completed").

            @stripe.on("payment.succeeded")
            async def on_paid(event: WebhookEvent):
                await db.mark_order_paid(event.data["object_id"])
        """
        def decorator(fn: Callable) -> Callable:
            self._handlers.setdefault(event_type, []).append(fn)
            return fn
        return decorator

    def register(self, event_type: str, handler: Callable) -> None:
        """Enregistre un handler sans décorateur."""
        self._handlers.setdefault(event_type, []).append(handler)

    # ------------------------------------------------------------------
    # Helpers privés
    # ------------------------------------------------------------------

    def _get_product(self, product_id: str) -> dict[str, Any]:
        if product_id not in self._products:
            raise ValueError(
                f"[stripe] Produit inconnu : '{product_id}'. "
                f"Disponibles : {sorted(self._products)}"
            )
        return self._products[product_id]

    async def _find_customer(self, email: str) -> dict[str, Any] | None:
        result = await _stripe.Customer.list_async(email=email, limit=1)
        if not result.data:
            return None
        c = result.data[0]
        return {"id": c.id, "email": c.email}