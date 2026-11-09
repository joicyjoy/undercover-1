"""AgentCore Memory, the agent's notes on each customer: the store a request graph
keeps them in when UNDERCOVER_MEMORY_ID is set, what it saves of a decided request,
and a search of one customer's memories."""

import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.base import BaseStore
from langgraph_checkpoint_aws import AgentCoreMemoryStore

MEMORY_ID = "UNDERCOVER_MEMORY_ID"
# Every memory of a customer is under /customers/<customer>, and the summary of
# each of their claims under /customers/<customer>/claims/<claim>/.
CUSTOMERS = "customers"
CLAIM_SUMMARY = f"/{CUSTOMERS}/{{customer_id}}/claims/{{claim_id}}/"
MOST_FOUND = 5
# What AgentCore Memory raises when it can't be reached or refuses a call.
UNREACHABLE = (BotoCoreError, ClientError)


def memory_store() -> BaseStore | None:
    """AgentCore Memory at UNDERCOVER_MEMORY_ID, read now, in the AWS CLI's region,
    or None when it isn't set."""
    memory_id = os.getenv(MEMORY_ID)
    if not memory_id:
        return None
    region = boto3.Session().region_name
    return AgentCoreMemoryStore(memory_id=memory_id, region_name=region)


def what_was_claimed(claim: dict[str, Any]) -> str:
    """The claim or request as its customer made it: its summary, the benefit claimed,
    the date and place of the incident and the items claimed, and never the story,
    customer message or documents."""
    req_id = (
        claim.get("claim_id")
        or claim.get("request_id")
        or claim.get("request", {}).get("request_id", "")
    )
    summary = (
        claim.get("summary")
        or claim.get("intent_summary")
        or claim.get("request", {}).get("intent_summary")
        or claim.get("request", {}).get("summary", "")
    )
    benefit = (
        claim.get("claim_type")
        or claim.get("request_type")
        or claim.get("type")
        or claim.get("request", {}).get("type", "")
    )
    date = (
        claim.get("incident_date")
        or claim.get("created_at")
        or claim.get("request", {}).get("created_at", "")
    )
    location = (
        claim.get("incident_location")
        or claim.get("location")
        or claim.get("order", {}).get("delivery_address", "")
        or "Shoppie Online"
    )
    raw_items = claim.get("items") or claim.get("order", {}).get("items") or []
    items_formatted = []
    for item in raw_items:
        desc = item.get("description") or item.get("name") or "Item"
        raw_amt = item.get("amount") if "amount" in item else item.get("price", 0.0)
        amount = float(raw_amt or 0.0)
        items_formatted.append(f"{desc}, ${amount:,.2f}")
    items = "; ".join(items_formatted)

    label = "Claim" if "claim_id" in claim else "Request"
    return (
        f"{label} {req_id}: {summary}\n"
        f"Benefit claimed: {benefit}\n"
        f"Incident: {date}, {location}\n"
        f"Items claimed: {items}"
    )


what_was_requested = what_was_claimed


def what_was_decided(claim: dict[str, Any]) -> str:
    """The decision on a claim or request, with its payout, section and reason."""
    req_id = (
        claim.get("claim_id")
        or claim.get("request_id")
        or claim.get("request", {}).get("request_id", "")
    )
    decision = claim.get("decision") or claim.get("recommendation") or {}
    kind = decision.get("decision") or decision.get("outcome", "")
    raw_payout = (
        decision.get("payout")
        if "payout" in decision
        else decision.get("refund_amount", 0.0)
    )
    payout = float(raw_payout or 0.0)
    section = decision.get("section") or (
        decision.get("policy_citations", [""])[0]
        if isinstance(decision.get("policy_citations"), list)
        and decision.get("policy_citations")
        else ""
    )
    reason = decision.get("reason") or decision.get("rationale", "")
    return (
        f"Decision on {req_id}: {kind}, paying "
        f"${payout:,.2f}, under {section}. "
        f"{reason}"
    )


def remember(store: BaseStore, claim: dict[str, Any]) -> str:
    """Save a decided claim or request to memory, with its customer as the actor and
    the claim/request as the session: the claim/request as the customer's message and
    the decision as the agent's. Return what was saved."""
    customer_id = claim.get("customer_id") or claim.get("customer", {}).get(
        "customer_id", ""
    )
    req_id = (
        claim.get("claim_id")
        or claim.get("request_id")
        or claim.get("request", {}).get("request_id", "")
    )
    actor_and_session = (customer_id, req_id)
    claimed, decided = what_was_claimed(claim), what_was_decided(claim)
    store.put(actor_and_session, "claim", {"message": HumanMessage(claimed)})
    store.put(actor_and_session, "decision", {"message": AIMessage(decided)})
    return f"{claimed}\n{decided}"


def recall(store: BaseStore, customer_id: str, claim_id: str, query: str) -> list[str]:
    """Search one customer's memories and return each one's text, leaving out the
    summary of the claim/request being worked on. AgentCoreMemoryStore gives every
    result the prefix searched as its namespace, so a memory's own namespaces are
    read from its value."""
    own = CLAIM_SUMMARY.format(customer_id=customer_id, claim_id=claim_id)
    found = store.search((CUSTOMERS, customer_id), query=query, limit=MOST_FOUND)
    return [
        item.value["content"]
        for item in found
        if own not in item.value["namespaces"]
        and f"/{claim_id}/" not in "".join(item.value.get("namespaces", []))
    ]


def what_was_found(query: str, found: list[str]) -> str:
    """Say what a search of the memory looked for and what it found."""
    if not found:
        return f'"{query}": nothing found'
    return "\n".join([f'"{query}"', *(f"- {text}" for text in found)])
