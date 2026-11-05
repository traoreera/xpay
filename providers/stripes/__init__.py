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
        self._handlers: dict[str, list[Callable]] = {}

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    async def _init(self) -> None:
        secret_key = self._config.get("secret_key")
        if not secret_key:
            # Mode dégradé : le backend démarre sans paiement en ligne ; chaque appel
            # de paiement échoue ensuite avec un message explicite (_require_ready).
            logger.warning(
                "[stripe] 'secret_key' manquant : paiement en ligne désactivé. "
                "Renseignez STRIPE_SECRET_KEY pour l'activer."
            )
            self._ready = False
            return

        _stripe.api_key = secret_key
        self._webhook_secret = self._config.get("webhook_secret", "")

        # Vérification de la connexion API : une panne réseau ou une clé refusée ne doit
        # pas empêcher le démarrage du reste de l'ERP.
        try:
            await _stripe.Account.retrieve_async()
        except Exception as exc:  # noqa: BLE001 - toute erreur Stripe/réseau = mode dégradé
            logger.warning(
                "[stripe] API injoignable ou clé refusée : paiement en ligne désactivé (%s)",
                type(exc).__name__,
            )
            self._ready = False
            return

        self._ready = True
        logger.info("[stripe] prêt (domain=%s)", self._domain())

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
            "webhook_handlers": {
                evt: len(handlers)
                for evt, handlers in self._handlers.items()
            },
        }

    # ------------------------------------------------------------------
    # Paiements one-time
    # ------------------------------------------------------------------

    def _callback_url(self, path: str, with_session: bool = False) -> str:
        """Construit une URL de retour Stripe.

        - chemin relatif ("/success") → joint au domaine configuré ;
        - URL absolue ("https://...") → utilisée telle quelle.

        Stripe n'accepte que http(s) : un schéma custom (erp://, app://) est
        refusé. On lève une erreur explicite plutôt que de laisser Stripe
        répondre « Not a valid URL ». Pour un deep-link, passez par une page
        https intermédiaire qui redirige ensuite vers votre schéma.
        """
        from urllib.parse import urlsplit

        url = path if urlsplit(path).scheme else f"{self._domain()}/{path.lstrip('/')}"
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(
                f"[stripe] URL de retour invalide '{url}' : Stripe exige http(s). "
                "Pour un deep-link (erp://, app://), utilisez une page https "
                "intermédiaire qui redirige vers votre schéma."
            )
        if with_session:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}session_id={{CHECKOUT_SESSION_ID}}"
        return url

    async def create_checkout(
        self,
        product_id: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:
        self._require_ready()

        # product_id est un identifiant Stripe (prod_xxx), fourni par xlicense
        # (source de vérité). On règle le paiement sur le prix par défaut du
        # produit : aucun catalogue local n'est nécessaire.
        product = await _stripe.Product.retrieve_async(product_id)
        price_id = product.get("default_price")
        if not price_id:
            raise ValueError(
                f"[stripe] Le produit '{product_id}' n'a pas de prix par défaut "
                "(default_price). Configurez-le dans Stripe."
            )
        session = await _stripe.checkout.Session.create_async(
            # `payment_method_types` n'est plus accepté par l'API Checkout : les moyens de paiement se
            # règlent dans le tableau de bord du compte (« dynamic payment methods »). Le passer fait
            # échouer TOUTE création de session — mesuré contre l'API réelle le 3 nov. 2026.
            line_items=[{"price": price_id, "quantity": 1}],
            mode="payment",
            success_url=self._callback_url(success_path, with_session=True),
            cancel_url=self._callback_url(cancel_path),
            metadata=metadata or {},
        )
        return CheckoutResult(
            url=session.url,
            session_id=session.id,
            provider=self.name,
            metadata={"product_id": product_id, "price_id": price_id},
        )

    async def list_catalog(self) -> list[dict[str, Any]]:
        """Liste les produits Stripe actifs avec leur prix par défaut.

        Renvoie pour chaque produit : product_id, name, price_id (du
        default_price) et price_type ('recurring' | 'one_time' | None).
        Sert au seed xpayproxy qui synchronise le mapping plan→Stripe en DB.
        """
        self._require_ready()

        resp = await _stripe.Product.list_async(
            active=True, limit=100, expand=["data.default_price"]
        )
        catalog: list[dict[str, Any]] = []
        for prod in resp.data:
            price = prod.get("default_price")
            if isinstance(price, dict):
                price_id = price.get("id")
                price_type = price.get("type")
            else:
                price_id = price  # juste l'id, non étendu
                price_type = None
            catalog.append({
                "product_id": prod.get("id"),
                "name": prod.get("name"),
                "price_id": price_id,
                "price_type": price_type,
            })
        return catalog

    async def verify_payment(self, session_id: str) -> PaymentStatus:
        self._require_ready()

        session = await _stripe.checkout.Session.retrieve_async(session_id)
        d = session.to_dict() if hasattr(session, "to_dict") else dict(session)
        paid = d.get("payment_status") == "paid"
        # Les métadonnées de la SESSION sont rendues telles quelles : c'est par elles que l'appelant
        # reconnaît ce qui a été acheté (pour qui, quel article), et c'est la seule chose qu'il puisse
        # croire — le navigateur, lui, n'apporte qu'un identifiant de session. Les écraser par le seul
        # `stripe_session` rendait toute vérification impossible : l'appelant ne pouvait que refuser.
        meta = dict(d.get("metadata") or {})
        meta["stripe_session"] = d.get("id")
        # L'abonnement créé par une session `mode=subscription` : sans lui, l'appelant devait le
        # rechercher par l'adresse du client, un détour qui échoue dès qu'un compte a plusieurs abonnements.
        if d.get("subscription"):
            meta["subscription_id"] = d["subscription"]
        details = d.get("customer_details") or {}
        return PaymentStatus(
            paid=paid,
            session_id=session_id,
            customer_email=details.get("email") if paid else None,
            amount=d.get("amount_total"),
            currency=d.get("currency"),
            metadata=meta,
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
            # `payment_method_types` n'est plus accepté par l'API Checkout : les moyens de paiement se
            # règlent dans le tableau de bord du compte (« dynamic payment methods »). Le passer fait
            # échouer TOUTE création de session — mesuré contre l'API réelle le 3 nov. 2026.
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=self._callback_url(success_path, with_session=True),
            cancel_url=self._callback_url(cancel_path),
            metadata=metadata or {},
            # Sans ceci, l'objet Subscription créé par Stripe a un metadata VIDE
            # (metadata=... ci-dessus ne le pose que sur la Session) — tenant_id/
            # license_id étaient alors introuvables dans les handlers
            # subscription.created/subscription.updated, qui lisent le metadata
            # de l'abonnement, pas de la session (audit xpayproxy Constat 5, bug 1).
            subscription_data={"metadata": metadata or {}},
        )
        return CheckoutResult(
            url=session.url,
            session_id=session.id,
            provider=self.name,
            metadata={"price_id": price_id, "customer_email": customer_email},
        )

    async def get_price(self, price_id: str) -> dict[str, Any]:
        """Le tarif tel que le prestataire le connaît : c'est LUI qui dit ce qui sera débité.

        Un catalogue local qui porterait un montant de son côté finirait par mentir — annoncer 19,99 € et
        prélever 1 000 € est précisément ce qui arrive quand les deux vivent séparément.
        """
        self._require_ready()

        price = await _stripe.Price.retrieve_async(price_id)
        d = price.to_dict() if hasattr(price, "to_dict") else dict(price)
        recurring = d.get("recurring") or {}
        amount = d.get("unit_amount")
        return {
            "price_id": d.get("id"),
            # Stripe compte en centimes ; le reste de la plateforme en unités.
            "amount": (amount / 100) if isinstance(amount, (int, float)) else None,
            "currency": (d.get("currency") or "").upper() or None,
            "interval": recurring.get("interval"),
            "recurring": bool(recurring),
            "active": bool(d.get("active")),
        }

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
            return_url=self._callback_url(return_path),
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


    async def _find_customer(self, email: str) -> dict[str, Any] | None:
        result = await _stripe.Customer.list_async(email=email, limit=1)
        if not result.data:
            return None
        c = result.data[0]
        return {"id": c.id, "email": c.email}