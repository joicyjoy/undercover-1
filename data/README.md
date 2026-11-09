# Shoppie pilot data

All customer, order, and support request records in this folder are synthetic. They are fictional development data, not live customer information.

- `customer_orders.json`: Shoppie's synthetic customers, orders, request records, and harness notes. This is the pilot's source dataset.
- `security/customer_orders.json`: one synthetic request, REQ-7901, whose message hides an instruction to the AI. Seeding adds it to the queue (9 requests in all) to exercise the security check. It has no reference decision in `evals/`.
- Evidence references marked `synthetic://` are fictional scenario records created for agent development. Customer follow-ups and operational updates are not real interactions or proof of real events.
- `evals/support_decisions.json`: illustrative reference decisions for evaluation. Keep this file out of agent tools, prompts, and runtime API responses.
- `../docs/shopping-policy.md`, `../docs/cancellation-policy.md`, and `../docs/returns-policy.md`: sample product, cancellation, return, replacement, and refund policies.

The sample policies and operational details are fictional and need review before any real customer use. Amounts use INR and times use IST.
