"""
PaymentProvider — Contrat abstrait pour les extensions de paiement xcore.

Implémentez cette classe pour intégrer n'importe quel provider
(Stripe, PayPal, Paddle, Braintree…) comme extension xcore.

Exemple d'implémentation :
    class StripeProvider(PaymentProvider):
        name = "stripe"

        async def init(self) -> None: ...
        async def create_checkout(self, ...) -> str: ...
        # etc.

Déclaration dans xcore.yaml :
    services:
      extensions:
        stripe:
          module: myapp.payments.stripe:StripeProvider
          config:
            secret_key: ${STRIPE_SECRET_KEY}
            domain: https://myapp.com
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Objets de transfert (domaine-neutre)
# ---------------------------------------------------------------------------

@dataclass
class CheckoutResult:
    """Résultat d'une session de paiement créée."""
    url: str
    session_id: str
    provider: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentStatus:
    """État d'un paiement après vérification."""
    paid: bool
    session_id: str
    customer_email: str | None = None
    amount: int | None = None        # centimes
    currency: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubscriptionStatus:
    """État d'un abonnement."""
    active: bool
    subscription_id: str | None = None
    customer_id: str | None = None
    current_period_end: int | None = None   # timestamp unix
    cancel_at_period_end: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    """Événement webhook normalisé (indépendant du provider)."""
    event_type: str          # ex: "payment.succeeded", "subscription.cancelled"
    provider: str            # "stripe", "paypal"…
    raw: Any                 # objet original du provider
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Contrat abstrait
# ---------------------------------------------------------------------------

class PaymentProvider(ABC):
    """
    Contrat abstrait pour un provider de paiement xcore.

    Cycle de vie (appelé par ExtensionLoader) :
        init()         → connexion, vérification des credentials
        shutdown()     → nettoyage
        health_check() → (ok: bool, message: str)
        status()       → dict d'état pour /health et /metrics

    Groupes fonctionnels à implémenter :
        Paiements one-time  → create_checkout(), verify_payment()
        Abonnements         → create_subscription_checkout(),
                              get_subscription(), cancel_subscription()
        Clients             → get_or_create_customer()
        Portail             → create_portal()
        Webhooks            → parse_webhook(), handle_webhook()
    """

    #: Identifiant du provider — utilisé comme clé dans le registry xcore.
    #: Doit être surchargé dans chaque implémentation.
    name: str = "payment_provider"

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._ready = False

    # ------------------------------------------------------------------
    # Cycle de vie (BaseService-compatible)
    # ------------------------------------------------------------------

    @abstractmethod
    async def _init(self) -> None:
        """
        Initialise la connexion au provider.
        Configure les credentials, vérifie l'accès à l'API.
        Doit positionner self._ready = True en cas de succès.
        """

    @abstractmethod
    async def _shutdown(self) -> None:
        """Libère les ressources (connexions, tâches background)."""

    @abstractmethod
    async def _health_check(self) -> tuple[bool, str]:
        """
        Vérifie que le provider est joignable.
        Retourne (True, "ok") ou (False, "message d'erreur").
        """

    @abstractmethod
    def _status(self) -> dict[str, Any]:
        """
        Retourne un snapshot de l'état du provider.
        Utilisé par xcore pour /health et /metrics.
        """

    # ------------------------------------------------------------------
    # Paiements one-time
    # ------------------------------------------------------------------

    @abstractmethod
    async def create_checkout(
        self,
        product_id: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:
        """
        Crée une session de paiement one-time.

        Args:
            product_id:   identifiant du produit dans le catalogue local.
            success_path: chemin de redirection après paiement réussi.
            cancel_path:  chemin de redirection en cas d'abandon.
            metadata:     données arbitraires à associer à la session.

        Returns:
            CheckoutResult avec l'URL de paiement et l'identifiant de session.
        """

    @abstractmethod
    async def verify_payment(self, session_id: str) -> PaymentStatus:
        """
        Vérifie l'état d'un paiement après retour du provider.

        Args:
            session_id: identifiant de session retourné par create_checkout().

        Returns:
            PaymentStatus indiquant si le paiement a bien été capturé.
        """

    # ------------------------------------------------------------------
    # Abonnements
    # ------------------------------------------------------------------

    @abstractmethod
    async def create_subscription_checkout(
        self,
        price_id: str,
        customer_email: str,
        success_path: str = "/success",
        cancel_path: str = "/cancel",
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutResult:
        """
        Crée une session de paiement pour un abonnement récurrent.

        Args:
            price_id:       identifiant du plan tarifaire chez le provider.
            customer_email: email du client (utilisé pour retrouver/créer le compte).
            success_path:   chemin de redirection après souscription.
            cancel_path:    chemin de redirection en cas d'abandon.
            metadata:       données arbitraires à associer à la session.
        """

    @abstractmethod
    async def get_subscription(self, customer_email: str) -> SubscriptionStatus:
        """
        Retourne l'état de l'abonnement actif d'un client.

        Lève ValueError si aucun client ou abonnement actif n'est trouvé.
        """

    @abstractmethod
    async def cancel_subscription(
        self,
        subscription_id: str,
        immediately: bool = False,
    ) -> SubscriptionStatus:
        """
        Annule un abonnement.

        Args:
            subscription_id: identifiant de l'abonnement chez le provider.
            immediately:     si True, annule immédiatement ;
                             sinon, à la fin de la période en cours.
        """

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_or_create_customer(self, email: str) -> dict[str, Any]:
        """
        Retourne le client existant ou en crée un nouveau.

        Returns:
            dict contenant au minimum {"id": str, "email": str}.
        """

    # ------------------------------------------------------------------
    # Portail de facturation
    # ------------------------------------------------------------------

    @abstractmethod
    async def create_portal(
        self,
        customer_email: str,
        return_path: str = "/dashboard",
    ) -> str:
        """
        Crée une session du portail de facturation self-service.

        Returns:
            URL du portail (valable pour une session unique).
        """

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_webhook(self, payload: bytes, signature: str) -> WebhookEvent:
        """
        Vérifie la signature du webhook et retourne un WebhookEvent normalisé.

        Lève une exception si la signature est invalide.

        Args:
            payload:   corps brut de la requête HTTP (bytes).
            signature: valeur de l'en-tête de signature (ex: Stripe-Signature).
        """

    @abstractmethod
    async def handle_webhook(self, event: WebhookEvent) -> None:
        """
        Dispatche l'événement webhook vers les handlers enregistrés.

        Args:
            event: WebhookEvent retourné par parse_webhook().
        """

    @abstractmethod
    def on(self, event_type: str):
        """
        Décorateur pour enregistrer un handler sur un type d'événement.

            @provider.on("payment.succeeded")
            async def on_paid(event: WebhookEvent): ...

        Args:
            event_type: type d'événement normalisé (défini par l'implémentation).
        """

    # ------------------------------------------------------------------
    # Helpers disponibles dans toutes les implémentations
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """Retourne True si le provider est initialisé et opérationnel."""
        return self._ready

    def _require_ready(self) -> None:
        """Lève RuntimeError si le provider n'est pas initialisé."""
        if not self._ready:
            raise RuntimeError(
                f"[{self.name}] Provider non initialisé. "
                "Appelez init() ou vérifiez la configuration dans xcore.yaml."
            )

    def _domain(self) -> str:
        """Retourne le domaine configuré, sans slash final."""
        return self._config.get("domain", "http://localhost:8000").rstrip("/")