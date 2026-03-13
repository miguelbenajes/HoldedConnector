# Brain Email Send — Design Spec

**Date:** 2026-03-13
**Status:** Draft
**Scope:** services/brain/

---

## Problem

Brain can read Gmail and create drafts, but cannot send emails. Holded document sending exists via `holded_action.send_document` but has no code-enforced approval gate — it relies solely on a system prompt instruction, which is fragile. Miguel wants Brain to:

1. Send free-form Gmail emails (from his account)
2. Send Holded documents via Holded's email system
3. Send Gmail emails with Holded invoice/estimate PDFs attached
4. All sends require Miguel's explicit in-chat approval (Telegram/Discord)

## Requirements

- **Inline approval** — Brain shows a preview in the conversation, Miguel replies yes/no
- **Code-enforced gate** — confirmation happens at the transport layer (Telegram/Discord message handler), completely outside the LLM's tool-calling loop. The LLM has no tool to confirm sends.
- **Three send types:** plain Gmail, Holded document, Gmail + Holded PDF attachment
- **No timeout** — pending sends wait indefinitely until approved or rejected
- **Proactive suggestions** — Brain may suggest sending emails when contextually appropriate, but the approval gate always applies
- **Lost on restart = safe** — pending sends are in-memory, not persisted
- **PDF size limit** — 10MB max for Gmail attachments

## Non-Goals

- Dashboard/web UI for approval (inline chat only)
- Attachments from sources other than Holded documents
- Auto-timeout or reminder nudges
- Persisting pending sends across restarts

---

## Architecture

### Key Security Decision: Confirmation Outside the LLM Loop

The LLM cannot confirm its own sends. The `confirm_send` action is NOT a tool — it's handled by the Telegram/Discord transport layer. When Miguel replies "yes" to a pending send, the message handler detects it and calls `executePendingSend()` directly, then injects the result back into the conversation. Even if the LLM is compromised by prompt injection, it has no tool to trigger the actual send.

```
User says "email the invoice to Juan"
    │
    ▼
Brain calls gmail({ action: "send", to, subject, body, doc_type?, doc_id? })
    │
    ▼
executeGmail "send" case
    │
    ├── doc_type+doc_id present? → type = "gmail_with_attachment"
    │   └── Validate: fetch doc status, reject if draft (status 0)
    └── no attachment?           → type = "gmail"
    │
    ▼
Validate all emails (format check on each address)
    │
    ▼
pendingSends.create({ id, type, params })
    │
    ▼
Returns preview string to Brain (ID is tracked internally, NOT shown to user)
    │
    ▼
Brain shows preview to Miguel in Telegram/Discord
    │
    ▼
Miguel replies "yes" / "sí" / "go" — OR — "no" / "cancel"
    │
    ▼
Transport layer (Telegram/Discord handler) detects reply to pending send
    │                                       ↑ NOT the LLM — the message handler code
    ▼
    ├── approved
    │   ├── gmail              → _sendEmail(to, subject, body)
    │   ├── gmail_with_attachment → downloadPdf() + _sendEmailWithAttachment()
    │   │   └── Guard: PDF must be ≤ 10MB
    │   └── holded_document    → sendDocument() via connector REST API (skip_confirm=true)
    │
    └── rejected → delete from map, inject "Cancelled" into conversation
```

For Holded document sends via `holded_action`:
```
Brain calls holded_action({ action: "send_document", doc_type, doc_id, emails, ... })
    │
    ▼
executeHoldedAction "send_document" case (MODIFIED)
    │
    ▼
Validate emails (each address in array), validate doc exists + not draft
    │
    ▼
pendingSends.create({ id, type: "holded_document", params })
    │
    ▼
Returns preview → Brain shows → Miguel replies → transport layer executes
```

**Holded connector interaction:** The connector's Safe Write Gateway is called with the existing `skip_confirm=true` pattern — Brain's pending-sends is the single approval gate. No double-confirmation.

---

## Components

### 1. `src/skills/pending-sends.ts` (NEW)

In-memory store for pending email sends. This module is the ONLY code that imports `_sendEmail` and `_sendEmailWithAttachment` from gmail.ts.

```typescript
interface PendingSend {
  id: string;                          // short random ID (8 chars, not UUID — clean for logs)
  type: "gmail" | "holded_document" | "gmail_with_attachment";
  params: GmailSendParams | HoldedDocSendParams | GmailAttachmentParams;
  createdAt: number;                   // Date.now()
}

interface GmailSendParams {
  to: string;
  subject: string;
  body: string;
  html?: boolean;
}

interface HoldedDocSendParams {
  doc_type: string;
  doc_id: string;
  emails: string[];                    // validated, at least 1
  subject?: string;
  body?: string;
}

interface GmailAttachmentParams extends GmailSendParams {
  doc_type: string;
  doc_id: string;
}
```

**Exports:**
- `createPendingSend(type, params)` → validates inputs, returns `{ id, preview: string }`
- `executePendingSend(id)` → executes the send, deletes from map, returns result string
- `cancelPendingSend(id)` → deletes from map, returns confirmation string
- `getPendingSend(id)` → returns PendingSend or null
- `getLatestPendingSend()` → returns the most recent pending send (for "yes" replies without explicit ID)
- `hasPendingSends()` → boolean check for transport layer

**Validation at queue time:**
- Email format validated per address (RFC 5321 pattern)
- For `holded_document`: at least 1 email, max 10
- For `gmail_with_attachment`: doc status fetched from connector — reject if draft (status 0)

### 2. `src/skills/google/gmail.ts` (MODIFIED)

**Make send functions non-exported.** Rename to underscore-prefixed internal functions:

```typescript
// INTERNAL — only callable via pending-sends.ts
async function _sendEmail(opts: {
  to: string;
  subject: string;
  body: string;
  html?: boolean;
}): Promise<{ messageId: string }>

// NEW — INTERNAL
async function _sendEmailWithAttachment(opts: {
  to: string;
  subject: string;
  body: string;
  html?: boolean;
  attachment: {
    filename: string;
    contentType: string;
    data: Buffer;          // raw PDF bytes, max 10MB
  };
}): Promise<{ messageId: string }>
```

**`_sendEmailWithAttachment` implementation:** Build multipart MIME message with boundary, text/html body part + application/pdf attachment part (base64-encoded), send via Gmail API `messages/send`.

**Export wrappers for pending-sends.ts only:**
```typescript
// Exposed to pending-sends.ts via explicit re-export object
export const gmailSendInternal = { sendEmail: _sendEmail, sendEmailWithAttachment: _sendEmailWithAttachment };
```

**Existing internal callers** (`blog.ts`, `email-monitor.ts`) are updated to use `gmailSendInternal.sendEmail()`.

### 3. `src/skills/holded.ts` (MODIFIED)

Add PDF download helper:

```typescript
export async function downloadDocumentPdf(
  doc_type: string,
  doc_id: string
): Promise<Buffer>
```

**Implementation:** `GET /api/entities/{doc_type}/{doc_id}/pdf` from Holded connector → return binary buffer. The connector's proxy endpoint returns raw bytes (already decoded from Holded's base64 JSON response).

**Size guard:** Reject if buffer exceeds 10MB (Gmail API limit is 25MB, but 10MB is a safe ceiling for email attachments).

### 4. `src/tools.ts` (MODIFIED)

**Gmail tool schema** — add `"send"` to action enum + optional `doc_type`, `doc_id` params:

```typescript
action: {
  type: "string",
  enum: ["inbox", "search", "read", "draft", "send"],
  description: "Action: inbox (recent), search (query), read (full email), draft (create draft), send (queue email for Miguel's approval — does NOT send immediately)"
},
// Existing params: to, subject, body
// New optional params for attachment:
doc_type: {
  type: "string",
  enum: ["invoice", "estimate", "purchase", "creditnote", "proforma"],
  description: "Holded document type to attach as PDF (optional — only for send action)"
},
doc_id: {
  type: "string",
  description: "Holded document ID to attach as PDF (optional — only for send action)"
},
html: {
  type: "boolean",
  description: "Send body as HTML (optional, default false)"
}
```

**Gmail executor** — new `case "send":` that queues via `pendingSends`:

```typescript
case "send": {
  const type = (args.doc_type && args.doc_id) ? "gmail_with_attachment" : "gmail";
  const result = createPendingSend(type, {
    to: args.to, subject: args.subject, body: args.body,
    html: args.html,
    ...(args.doc_type && { doc_type: args.doc_type, doc_id: args.doc_id })
  });
  if (result.error) return { content: result.error, error: true };
  return { content: result.preview };
}
```

**Holded executor** — modify `send_document` case to queue instead of executing:

```typescript
case "send_document": {
  const result = createPendingSend("holded_document", {
    doc_type: args.doc_type, doc_id: args.doc_id,
    emails: args.emails, subject: args.subject, body: args.body
  });
  if (result.error) return { content: result.error, error: true };
  return { content: result.preview };
}
```

**NO `confirm_send` tool.** The LLM has no way to execute pending sends.

### 5. Transport Layer: `src/routes/telegram.ts` + `src/routes/discord.ts` (MODIFIED)

Add pending-send detection to the message handler, **before** passing the message to the agent:

```typescript
import { hasPendingSends, getLatestPendingSend, executePendingSend, cancelPendingSend } from "../skills/pending-sends";

// In the message handler, before calling agent:
if (hasPendingSends()) {
  const text = message.text.toLowerCase().trim();
  const isApproval = /^(yes|sí|si|go|send|ok|dale|venga)$/i.test(text);
  const isRejection = /^(no|cancel|cancelar|nope|stop)$/i.test(text);

  if (isApproval) {
    const pending = getLatestPendingSend();
    if (pending) {
      const result = await executePendingSend(pending.id);
      // Send result directly to chat (not through LLM)
      await sendMessage(chatId, result);
      // Inject into conversation history so Brain knows what happened
      await injectSystemMessage(conversationId, `[Email send result: ${result}]`);
      return; // Don't pass to agent
    }
  }

  if (isRejection) {
    const pending = getLatestPendingSend();
    if (pending) {
      const result = cancelPendingSend(pending.id);
      await sendMessage(chatId, result);
      await injectSystemMessage(conversationId, `[Email send cancelled by user]`);
      return;
    }
  }
}
// Otherwise, pass to agent as normal
```

### 6. `src/agent.ts` (MODIFIED)

Add to system prompt:

```
EMAIL CAPABILITIES:
- You can send emails via Gmail (free-form, from Miguel's account)
- You can send documents via Holded's email system (PDF auto-attached)
- You can send Gmail emails WITH Holded document PDFs attached —
  just include doc_type and doc_id when sending via Gmail
- You may proactively suggest sending emails when contextually appropriate
  (e.g., "Want me to email this invoice to the client?")

EMAIL FLOW: For ALL email sends:
1. Call the send tool (gmail action:"send" or holded_action action:"send_document")
2. The tool returns a preview — show it to Miguel exactly as returned
3. Miguel will reply YES or NO — this is handled automatically by the system
4. You will receive a system message with the result — acknowledge it to Miguel
5. You do NOT have a confirmation tool. The system handles approval directly.
```

---

## Preview Format

Returned by `createPendingSend()` for Brain to relay (ID is internal, not shown):

### Gmail (plain)
```
📧 Email queued for approval:
To: juan@example.com
Subject: Quote for camera rental
Body: Hi Juan, here's the quote we discussed...

Reply YES to send, NO to cancel.
```

### Gmail + Attachment
```
📧 Email queued for approval:
To: juan@example.com
Subject: Invoice INV-2026-0042
Body: Hi Juan, please find attached...
📎 Attachment: Invoice INV-2026-0042 (PDF from Holded)

Reply YES to send, NO to cancel.
```

### Holded Document
```
📄 Document send queued for approval:
Document: Invoice INV-2026-0042
Via: Holded email system
To: juan@example.com, billing@mediaset.es
Subject: (default Holded template)

Reply YES to send, NO to cancel.
```

---

## Error Handling

| Error | Handling |
|-------|----------|
| Invalid email format | Rejected at queue time, tool returns error to Brain |
| Holded doc is draft (status 0) | Rejected at queue time for holded_document and gmail_with_attachment |
| PDF download fails on execute | Transport layer sends error to chat, pending send stays for retry |
| PDF exceeds 10MB | Rejected at execute time, error sent to chat |
| Gmail API send fails | Transport layer sends error to chat with details |
| Holded connector send fails | Transport layer sends error to chat with details |
| No pending send found | "yes"/"no" reply passes through to agent as normal message |

---

## Files Changed

| File | Change |
|------|--------|
| `src/skills/pending-sends.ts` | **NEW** — PendingSend store, validation, preview formatting, execution |
| `src/skills/google/gmail.ts` | Make `sendEmail` non-exported, add `_sendEmailWithAttachment`, export via `gmailSendInternal` object |
| `src/skills/holded.ts` | Add `downloadDocumentPdf()` with 10MB size guard |
| `src/tools.ts` | Gmail enum + send case, holded send_document wrapping. NO confirm_send tool |
| `src/routes/telegram.ts` | Pending-send detection before agent dispatch |
| `src/routes/discord.ts` | Same pending-send detection |
| `src/agent.ts` | System prompt: email capabilities + flow description |

---

## Testing Plan

1. **Unit: pending-sends.ts** — create, get, execute, cancel, validation errors, duplicate handling
2. **Unit: _sendEmailWithAttachment** — verify MIME structure, boundary, base64 encoding, 10MB guard
3. **Unit: email validation** — valid addresses pass, invalid rejected, array validation for Holded sends
4. **Integration: Gmail send** — queue → transport approves → verify email sent via Gmail API
5. **Integration: Gmail + attachment** — queue → transport approves → verify PDF fetched + attached + sent
6. **Integration: Holded send** — queue → transport approves → verify connector API called with skip_confirm
7. **E2E: Telegram flow** — tell Brain to send email → see preview → reply "sí" → confirm sent
8. **Safety: reject flow** — queue → reply "no" → verify not sent + cleaned up
9. **Safety: no confirm tool** — verify LLM tool list does NOT include any confirmation mechanism
10. **Safety: direct import blocked** — verify `sendEmail` is not exported from gmail.ts (only `gmailSendInternal`)
11. **Edge: "yes" with no pending** — verify message passes through to agent normally
12. **Edge: PDF too large** — verify 10MB guard rejects with clear error
13. **Edge: draft document** — verify queue rejects send for status-0 documents
