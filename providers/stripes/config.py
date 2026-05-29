from ..base import WebhookEvent


_EVENT_MAP: dict[str, str] = {
    "checkout.session.completed":        "payment.succeeded",
    "checkout.session.expired":          "payment.expired",
    "invoice.payment_failed":            "payment.failed",
    "customer.subscription.created":     "subscription.created",
    "customer.subscription.updated":     "subscription.updated",
    "customer.subscription.deleted":     "subscription.cancelled",
    "customer.subscription.paused":      "subscription.paused",
    "customer.subscription.resumed":     "subscription.resumed",
    "invoice.upcoming":                  "invoice.upcoming",
    "invoice.paid":                      "invoice.paid",
}


def _normalize_event(raw_event) -> WebhookEvent:
    """Convertit un stripe.Event en WebhookEvent normalisé."""
    stripe_type: str = raw_event["type"]
    obj: dict = raw_event["data"]["object"]

    return WebhookEvent(
        event_type=_EVENT_MAP.get(stripe_type, stripe_type),
        provider="stripe",
        raw=raw_event,
        data={
            "stripe_event_type": stripe_type,
            "object_id": obj.get("id"),
            "customer_id": obj.get("customer"),
            "customer_email": (
                obj.get("customer_details", {}).get("email")
                or obj.get("customer_email")
            ),
            "amount": obj.get("amount_total") or obj.get("amount_paid"),
            "currency": obj.get("currency"),
            "status": obj.get("status") or obj.get("payment_status"),
            "subscription_id": obj.get("subscription") or (
                obj.get("id") if stripe_type.startswith("customer.subscription") else None
            ),
        },
    )

