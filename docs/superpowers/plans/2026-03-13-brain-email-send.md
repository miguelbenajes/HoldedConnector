# Brain Email Send — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Brain to send emails (Gmail, Holded documents, Gmail+PDF attachment) with a transport-layer approval gate that the LLM cannot bypass.

**Architecture:** New `pending-sends.ts` module stores queued sends in-memory. Tool executors queue sends and return previews. Telegram/Discord message handlers intercept yes/no replies and execute or cancel sends directly — outside the LLM loop. Gmail's `sendEmail` is made non-exported to prevent direct access.

**Tech Stack:** TypeScript, Gmail API (multipart MIME), Holded connector REST API

**Spec:** `docs/superpowers/specs/2026-03-13-brain-email-send-design.md`

---

## Chunk 1: Pending Sends Store + Gmail Send Unlock

### Task 1: Create `pending-sends.ts` module

**Files:**
- Create: `services/brain/src/skills/pending-sends.ts`

- [ ] **Step 1: Create the pending-sends module with types and store**

```typescript
// services/brain/src/skills/pending-sends.ts

/**
 * Pending Email Sends — In-memory queue with transport-layer approval gate.
 *
 * The LLM queues sends via tool calls, but CANNOT execute them.
 * Only the Telegram/Discord message handler can call executePendingSend()
 * after detecting Miguel's explicit "yes" reply.
 *
 * Pending sends are in-memory (Map). Lost on restart = safe default.
 */

import { randomBytes } from "crypto";
import { gmailSendInternal } from "./google/gmail.js";
import { sendDocument, downloadDocumentPdf } from "./holded.js";
import { holdedFetch } from "./holded.js";

// ─── Types ───────────────────────────────────────────────────────────

export interface GmailSendParams {
  to: string;
  subject: string;
  body: string;
  html?: boolean;
}

export interface HoldedDocSendParams {
  doc_type: string;
  doc_id: string;
  emails: string[];
  subject?: string;
  body?: string;
}

export interface GmailAttachmentParams extends GmailSendParams {
  doc_type: string;
  doc_id: string;
}

export type SendType = "gmail" | "holded_document" | "gmail_with_attachment";

interface PendingSend {
  id: string;
  type: SendType;
  params: GmailSendParams | HoldedDocSendParams | GmailAttachmentParams;
  createdAt: number;
}

// ─── Store ───────────────────────────────────────────────────────────

const pending = new Map<string, PendingSend>();

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function generateId(): string {
  return randomBytes(4).toString("hex"); // 8-char hex ID
}

// ─── Validation ──────────────────────────────────────────────────────

function validateEmail(email: string): string | null {
  if (!EMAIL_RE.test(email)) return `Invalid email address: "${email}"`;
  return null;
}

function validateEmails(emails: string[]): string | null {
  if (!emails.length) return "At least one email address is required";
  if (emails.length > 10) return "Maximum 10 email addresses allowed";
  for (const e of emails) {
    const err = validateEmail(e);
    if (err) return err;
  }
  return null;
}

// ─── Preview Formatting ──────────────────────────────────────────────

function truncate(text: string, max: number): string {
  return text.length > max ? text.slice(0, max) + "..." : text;
}

function formatGmailPreview(params: GmailSendParams): string {
  return [
    "📧 Email queued for approval:",
    `To: ${params.to}`,
    `Subject: ${params.subject}`,
    `Body: ${truncate(params.body, 200)}`,
    "",
    "Reply YES to send, NO to cancel.",
  ].join("\n");
}

function formatGmailAttachmentPreview(params: GmailAttachmentParams): string {
  return [
    "📧 Email queued for approval:",
    `To: ${params.to}`,
    `Subject: ${params.subject}`,
    `Body: ${truncate(params.body, 200)}`,
    `📎 Attachment: ${params.doc_type} PDF from Holded (ID: ${params.doc_id})`,
    "",
    "Reply YES to send, NO to cancel.",
  ].join("\n");
}

function formatHoldedDocPreview(params: HoldedDocSendParams): string {
  return [
    "📄 Document send queued for approval:",
    `Document: ${params.doc_type} (ID: ${params.doc_id})`,
    "Via: Holded email system",
    `To: ${params.emails.join(", ")}`,
    params.subject ? `Subject: ${params.subject}` : "Subject: (default Holded template)",
    "",
    "Reply YES to send, NO to cancel.",
  ].join("\n");
}

// ─── Public API ──────────────────────────────────────────────────────

/**
 * Check if a Holded document is a draft (status 0). Drafts cannot be sent.
 * Fetches document details from the Holded connector.
 */
async function checkDocNotDraft(doc_type: string, doc_id: string): Promise<string | null> {
  try {
    // Map send doc_type to entity table name for lookup
    const tableMap: Record<string, string> = {
      invoice: "invoices", purchase: "purchases", estimate: "estimates",
      creditnote: "invoices", proforma: "estimates",
    };
    const table = tableMap[doc_type];
    if (!table) return `Unknown doc_type: ${doc_type}`;
    const data = await holdedFetch(`/api/entities/${table}/${doc_id}`) as any;
    if (data?.status === 0) {
      return `Cannot send a draft document. Approve it first before sending.`;
    }
    return null;
  } catch {
    // If we can't check, allow queuing — the send will fail at execution if there's a real issue
    return null;
  }
}

/**
 * Queue an email send for approval. Returns preview string for Brain to show.
 * Async because it validates doc status for attachment/holded sends.
 */
export async function createPendingSend(
  type: SendType,
  params: GmailSendParams | HoldedDocSendParams | GmailAttachmentParams
): Promise<{ id: string; preview: string } | { error: string }> {
  // Validate emails based on type
  if (type === "gmail" || type === "gmail_with_attachment") {
    const p = params as GmailSendParams;
    const err = validateEmail(p.to);
    if (err) return { error: err };
    if (!p.subject?.trim()) return { error: "Email subject is required" };
    if (!p.body?.trim()) return { error: "Email body is required" };
  }
  if (type === "holded_document") {
    const p = params as HoldedDocSendParams;
    const err = validateEmails(p.emails);
    if (err) return { error: err };
  }

  // Reject draft documents — they must be approved before sending
  if (type === "gmail_with_attachment") {
    const p = params as GmailAttachmentParams;
    const draftErr = await checkDocNotDraft(p.doc_type, p.doc_id);
    if (draftErr) return { error: draftErr };
  }
  if (type === "holded_document") {
    const p = params as HoldedDocSendParams;
    const draftErr = await checkDocNotDraft(p.doc_type, p.doc_id);
    if (draftErr) return { error: draftErr };
  }

  const id = generateId();
  const send: PendingSend = { id, type, params, createdAt: Date.now() };
  pending.set(id, send);

  let preview: string;
  switch (type) {
    case "gmail":
      preview = formatGmailPreview(params as GmailSendParams);
      break;
    case "gmail_with_attachment":
      preview = formatGmailAttachmentPreview(params as GmailAttachmentParams);
      break;
    case "holded_document":
      preview = formatHoldedDocPreview(params as HoldedDocSendParams);
      break;
  }

  return { id, preview };
}

/**
 * Execute a pending send. Called by transport layer ONLY (not the LLM).
 * Downloads PDF if needed, sends via Gmail or Holded, cleans up.
 */
export async function executePendingSend(id: string): Promise<string> {
  const send = pending.get(id);
  if (!send) return "No pending send found with that ID.";

  try {
    switch (send.type) {
      case "gmail": {
        const p = send.params as GmailSendParams;
        const result = await gmailSendInternal.sendEmail(p);
        pending.delete(id);
        return `✅ Email sent to ${p.to} (ID: ${result.messageId})`;
      }

      case "gmail_with_attachment": {
        const p = send.params as GmailAttachmentParams;
        const pdf = await downloadDocumentPdf(p.doc_type, p.doc_id);
        const result = await gmailSendInternal.sendEmailWithAttachment({
          to: p.to,
          subject: p.subject,
          body: p.body,
          html: p.html,
          attachment: {
            filename: `${p.doc_type}-${p.doc_id}.pdf`,
            contentType: "application/pdf",
            data: pdf,
          },
        });
        pending.delete(id);
        return `✅ Email sent to ${p.to} with PDF attached (ID: ${result.messageId})`;
      }

      case "holded_document": {
        const p = send.params as HoldedDocSendParams;
        const result = await sendDocument(p);
        pending.delete(id);
        // sendDocument returns a formatted string from the gateway
        return result;
      }
    }
  } catch (err) {
    const msg = (err as Error).message || "Unknown error";
    // Don't delete — allow retry
    return `❌ Send failed: ${msg}. The pending send is still queued — reply YES to retry.`;
  }
}

/**
 * Cancel a pending send. Called by transport layer on "no" reply.
 */
export function cancelPendingSend(id: string): string {
  const send = pending.get(id);
  if (!send) return "No pending send found.";
  pending.delete(id);
  return "🚫 Email send cancelled.";
}

/** Get a pending send by ID. */
export function getPendingSend(id: string): PendingSend | null {
  return pending.get(id) ?? null;
}

/** Get the most recent pending send (for "yes" without explicit ID). */
export function getLatestPendingSend(): PendingSend | null {
  let latest: PendingSend | null = null;
  for (const send of pending.values()) {
    if (!latest || send.createdAt > latest.createdAt) latest = send;
  }
  return latest;
}

/** Check if there are any pending sends. */
export function hasPendingSends(): boolean {
  return pending.size > 0;
}
```

- [ ] **Step 2: Verify the file compiles**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit src/skills/pending-sends.ts 2>&1 | head -20`
Expected: No errors (or only errors from missing imports that will be created in later tasks)

- [ ] **Step 3: Commit**

```bash
git add src/skills/pending-sends.ts
git commit -m "feat(brain): add pending-sends store for email approval gate"
```

---

### Task 2: Make `sendEmail` non-exported + add `sendEmailWithAttachment`

**Files:**
- Modify: `services/brain/src/skills/google/gmail.ts:231-298`
- Modify: `services/brain/src/skills/blog.ts:25` (update import)
- Modify: `services/brain/src/tools.ts:64-68` (update import — only if sendEmail was imported there, but it's not)

- [ ] **Step 1: Rename `sendEmail` to `_sendEmail` (non-exported) in gmail.ts**

In `services/brain/src/skills/google/gmail.ts`, change line 231:
```typescript
// BEFORE:
export async function sendEmail(opts: {

// AFTER:
async function _sendEmail(opts: {
```

Everything else in the function stays identical.

- [ ] **Step 2: Add `_sendEmailWithAttachment` function after `_sendEmail`**

Insert after `_sendEmail` (after line ~298):

```typescript
/**
 * Send an email with a file attachment via Gmail API (multipart MIME).
 * INTERNAL — only callable via pending-sends.ts through gmailSendInternal.
 */
async function _sendEmailWithAttachment(opts: {
  to: string;
  subject: string;
  body: string;
  html?: boolean;
  attachment: {
    filename: string;
    contentType: string;
    data: Buffer;
  };
}): Promise<{ messageId: string }> {
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(opts.to)) {
    throw new Error("Invalid recipient email address");
  }

  const h = (v: string) => v.replace(/[\r\n\t]/g, " ").slice(0, 998);
  const boundary = `boundary_${Date.now()}_${Math.random().toString(36).slice(2)}`;

  const contentType = opts.html
    ? "text/html; charset=UTF-8"
    : "text/plain; charset=UTF-8";

  const parts = [
    `To: ${h(opts.to)}`,
    `Subject: ${h(opts.subject)}`,
    `MIME-Version: 1.0`,
    `Content-Type: multipart/mixed; boundary="${boundary}"`,
    "",
    `--${boundary}`,
    `Content-Type: ${contentType}`,
    "",
    opts.body,
    `--${boundary}`,
    `Content-Type: ${opts.attachment.contentType}; name="${opts.attachment.filename}"`,
    `Content-Disposition: attachment; filename="${opts.attachment.filename}"`,
    `Content-Transfer-Encoding: base64`,
    "",
    opts.attachment.data.toString("base64"),
    `--${boundary}--`,
  ];

  const raw = Buffer.from(parts.join("\r\n"), "utf-8").toString("base64url");

  const result = await googleFetch<{ id: string; threadId: string }>(
    `${GMAIL_BASE}/messages/send`,
    { method: "POST", body: JSON.stringify({ raw }) }
  );

  return { messageId: result.id };
}
```

- [ ] **Step 3: Add `gmailSendInternal` export object at the end of gmail.ts**

Add before the final closing of the module (after all function definitions):

```typescript
/**
 * Internal send functions — only for use by pending-sends.ts.
 * Not exported individually to prevent direct LLM tool access.
 */
export const gmailSendInternal = {
  sendEmail: _sendEmail,
  sendEmailWithAttachment: _sendEmailWithAttachment,
};
```

- [ ] **Step 4: Update blog.ts import**

In `services/brain/src/skills/blog.ts`, line 25:
```typescript
// BEFORE:
import { sendEmail } from "./google/gmail.js";

// AFTER:
import { gmailSendInternal } from "./google/gmail.js";
```

And line 1224:
```typescript
// BEFORE:
const result = await sendEmail({

// AFTER:
const result = await gmailSendInternal.sendEmail({
```

- [ ] **Step 5: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/skills/google/gmail.ts src/skills/blog.ts
git commit -m "feat(brain): make sendEmail non-exported, add sendEmailWithAttachment"
```

---

### Task 3: Add `downloadDocumentPdf` to holded.ts

**Files:**
- Modify: `services/brain/src/skills/holded.ts` (after line ~601, after `sendDocument`)

- [ ] **Step 1: Add the `downloadDocumentPdf` function**

Insert after the `sendDocument` function:

```typescript
const MAX_PDF_SIZE = 10 * 1024 * 1024; // 10MB

/**
 * Download a document PDF from the Holded connector.
 * Returns raw PDF bytes as a Buffer.
 * Rejects if PDF exceeds 10MB (Gmail attachment safety limit).
 */
export async function downloadDocumentPdf(
  doc_type: string,
  doc_id: string
): Promise<Buffer> {
  const idError = validateHoldedId(doc_id, "doc_id");
  if (idError) throw new Error(idError);

  if (!VALID_SEND_DOC_TYPES.has(doc_type)) {
    throw new Error(`Invalid doc_type "${doc_type}". Must be one of: ${[...VALID_SEND_DOC_TYPES].join(", ")}`);
  }

  if (!HOLDED_CONNECTOR_TOKEN) {
    throw new Error("HOLDED_CONNECTOR_TOKEN not configured");
  }

  // Fetch PDF from holded-connector proxy endpoint (returns raw bytes)
  const res = await fetch(`${HOLDED_API_URL}/api/entities/${doc_type}/${doc_id}/pdf`, {
    headers: { Authorization: `Bearer ${HOLDED_CONNECTOR_TOKEN}` },
    signal: AbortSignal.timeout(30_000),
  });

  if (!res.ok) {
    throw new Error(`PDF download failed: HTTP ${res.status}`);
  }

  const buffer = Buffer.from(await res.arrayBuffer());

  if (buffer.length > MAX_PDF_SIZE) {
    throw new Error(`PDF too large (${(buffer.length / 1024 / 1024).toFixed(1)}MB). Max: 10MB`);
  }

  return buffer;
}
```

- [ ] **Step 2: Ensure `HOLDED_API_URL` and `HOLDED_CONNECTOR_TOKEN` are accessible**

These should already be defined at the top of holded.ts. Verify:
Run: `grep -n "HOLDED_API_URL\|HOLDED_CONNECTOR_TOKEN" "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain/src/skills/holded.ts" | head -5`
Expected: Both constants defined near the top of the file.

- [ ] **Step 3: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add src/skills/holded.ts
git commit -m "feat(brain): add downloadDocumentPdf helper with 10MB guard"
```

---

### Task 4: Update Gmail tool schema + executor in tools.ts

**Files:**
- Modify: `services/brain/src/tools.ts:2348-2390` (gmail schema)
- Modify: `services/brain/src/tools.ts:1369-1438` (executeGmail)
- Modify: `services/brain/src/tools.ts:64-68` (imports)

- [ ] **Step 1: Add import for pending-sends at the top of tools.ts**

After the existing gmail imports (line 68), add:

```typescript
import {
  createPendingSend,
} from "./skills/pending-sends.js";
```

- [ ] **Step 2: Update gmail tool schema — add "send" to action enum + new params**

In the gmail tool schema (~line 2354), update the `action` enum and add new properties:

```typescript
        action: {
          type: "string",
          enum: ["inbox", "search", "read", "draft", "send"],
          description:
            "Action: inbox (recent emails), search (Gmail query), read (full email by ID), draft (create draft), send (queue email for approval — does NOT send immediately, Miguel must approve first)",
        },
```

Add these properties to the gmail tool schema's `properties` object (after the existing `limit` property):

```typescript
        doc_type: {
          type: "string",
          enum: ["invoice", "purchase", "estimate", "creditnote", "proforma"],
          description: "For 'send': Holded document type to attach as PDF (optional — omit for plain email)",
        },
        doc_id: {
          type: "string",
          description: "For 'send': Holded document ID to attach as PDF (optional — omit for plain email)",
        },
        html: {
          type: "boolean",
          description: "For 'send': send body as HTML instead of plain text (optional, default false)",
        },
```

- [ ] **Step 3: Add `case "send":` to executeGmail**

In the `executeGmail` function, add this case before the `default:` case (~line 1430):

```typescript
      case "send": {
        if (!args.to || !args.subject || !args.body) {
          return { content: "Need to, subject, and body to send an email.", error: true };
        }
        const type = (args.doc_type && args.doc_id) ? "gmail_with_attachment" as const : "gmail" as const;
        const params = type === "gmail_with_attachment"
          ? { to: args.to, subject: args.subject, body: args.body, html: args.html, doc_type: args.doc_type!, doc_id: args.doc_id! }
          : { to: args.to, subject: args.subject, body: args.body, html: args.html };
        const result = await createPendingSend(type, params);
        if ("error" in result) return { content: result.error, error: true };
        return { content: result.preview };
      }
```

Update the `default:` error message to include "send":
```typescript
      default:
        return { content: `Unknown Gmail action: ${args.action}. Use: inbox, search, read, draft, send`, error: true };
```

- [ ] **Step 4: Update executeGmail function signature to include new args**

Update the args type (~line 1369):
```typescript
async function executeGmail(args: {
  action: string;
  query?: string;
  message_id?: string;
  to?: string;
  subject?: string;
  body?: string;
  cc?: string;
  in_reply_to?: string;
  limit?: number;
  unread_only?: boolean;
  doc_type?: string;
  doc_id?: string;
  html?: boolean;
}): Promise<ToolResult> {
```

- [ ] **Step 5: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/tools.ts
git commit -m "feat(brain): unlock gmail send action with pending-sends queue"
```

---

### Task 5: Wrap Holded `send_document` through pending-sends

**Files:**
- Modify: `services/brain/src/tools.ts:893-905` (send_document case in executeHoldedAction)

- [ ] **Step 1: Replace the send_document case in executeHoldedAction**

Replace the existing `case "send_document":` block (~lines 893-905):

```typescript
      case "send_document": {
        const err = validateRequired(args, ["doc_type", "doc_id"], "send_document");
        if (err) return err;
        if (!args.emails?.length) {
          return { content: "Error: send_document requires at least one email address in 'emails' array", error: true };
        }
        const result = await createPendingSend("holded_document", {
          doc_type: args.doc_type,
          doc_id: args.doc_id,
          emails: args.emails,
          subject: args.subject,
          body: args.body,
        });
        if ("error" in result) return { content: result.error, error: true };
        return { content: result.preview };
      }
```

- [ ] **Step 2: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add src/tools.ts
git commit -m "feat(brain): route holded send_document through pending-sends queue"
```

---

## Chunk 2: Transport Layer + System Prompt

### Task 6: Add pending-send detection to Telegram handler

**Files:**
- Modify: `services/brain/src/routes/telegram.ts:371-410` (processBrainMessage)

- [ ] **Step 1: Add imports at the top of telegram.ts**

Add to the imports section:

```typescript
import {
  hasPendingSends,
  getLatestPendingSend,
  executePendingSend,
  cancelPendingSend,
} from "../skills/pending-sends.js";
```

- [ ] **Step 2: Add pending-send interception at the start of processBrainMessage**

Insert at the beginning of `processBrainMessage` (after line 371, before the existing `const channel = "telegram";`):

```typescript
  // ─── Pending email send approval gate ─────────────────────────────
  // Intercept yes/no replies BEFORE passing to the LLM agent.
  // This is the code-enforced gate: the LLM has no tool to confirm sends.
  if (hasPendingSends()) {
    const normalized = text.toLowerCase().trim();
    const isApproval = /^(y(es)?|sí?|go|send|ok|dale|venga|hazlo|envía(lo)?)$/.test(normalized);
    const isRejection = /^(no|cancel(ar)?|nope|stop|para|nah)$/.test(normalized);

    if (isApproval || isRejection) {
      const latest = getLatestPendingSend();
      if (latest) {
        const result = isApproval
          ? await executePendingSend(latest.id)
          : cancelPendingSend(latest.id);
        await reply(chatId, result);
        // Save to history so Brain sees what happened
        await saveMessage("telegram", String(chatId), "user", text);
        await saveMessage("telegram", String(chatId), "assistant", result);
        return;
      }
    }
  }
  // ─── End pending send gate ────────────────────────────────────────
```

- [ ] **Step 3: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add src/routes/telegram.ts
git commit -m "feat(brain): add pending-send approval gate to Telegram handler"
```

---

### Task 7: Add pending-send detection to Discord handler

**Files:**
- Modify: `services/brain/src/routes/discord.ts:73-102` (processBrainMessage)

- [ ] **Step 1: Add imports at the top of discord.ts**

Add to the imports section:

```typescript
import {
  hasPendingSends,
  getLatestPendingSend,
  executePendingSend,
  cancelPendingSend,
} from "../skills/pending-sends.js";
```

- [ ] **Step 2: Add pending-send interception at the start of processBrainMessage**

Insert at the beginning of `processBrainMessage` (after line 73, before the existing `const channel = "discord";`):

```typescript
  // ─── Pending email send approval gate ─────────────────────────────
  if (hasPendingSends()) {
    const normalized = text.toLowerCase().trim();
    const isApproval = /^(y(es)?|sí?|go|send|ok|dale|venga|hazlo|envía(lo)?)$/.test(normalized);
    const isRejection = /^(no|cancel(ar)?|nope|stop|para|nah)$/.test(normalized);

    if (isApproval || isRejection) {
      const latest = getLatestPendingSend();
      if (latest) {
        const result = isApproval
          ? await executePendingSend(latest.id)
          : cancelPendingSend(latest.id);
        await sendFollowUp(interactionToken, result);
        await saveMessage("discord", channelId, "user", text);
        await saveMessage("discord", channelId, "assistant", result);
        return;
      }
    }
  }
  // ─── End pending send gate ────────────────────────────────────────
```

- [ ] **Step 3: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add src/routes/discord.ts
git commit -m "feat(brain): add pending-send approval gate to Discord handler"
```

---

### Task 8: Update Brain system prompt

**Files:**
- Modify: `services/brain/src/agent.ts:77-82` (write safety section of BASE_SYSTEM_PROMPT)

- [ ] **Step 1: Add email capabilities and flow to the system prompt**

Find the existing write safety section in `BASE_SYSTEM_PROMPT` (~line 77). After the existing `HACIENDA/SII DANGER` paragraph, add:

```
  EMAIL CAPABILITIES:
  - You can send emails via Gmail (free-form, from Miguel's account) using gmail action:"send"
  - You can send documents via Holded's email system using holded_action action:"send_document"
  - You can send Gmail emails WITH Holded document PDFs attached — include doc_type and doc_id when using gmail action:"send"
  - You may proactively suggest sending emails when contextually appropriate (e.g., "Want me to email this invoice to the client?")

  EMAIL FLOW: For ALL email sends:
  1. Call the send tool (gmail action:"send" or holded_action action:"send_document")
  2. The tool returns a preview — show it to Miguel exactly as returned
  3. Miguel will reply YES or NO — this is handled automatically by the system
  4. You will see the result in conversation history — acknowledge it to Miguel
  5. You do NOT have a confirmation tool. The system handles approval directly.
```

- [ ] **Step 2: Verify compilation**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add src/agent.ts
git commit -m "feat(brain): add email capabilities and flow to system prompt"
```

---

### Task 9: Full build + manual smoke test

**Files:** None (verification only)

- [ ] **Step 1: Full TypeScript compilation check**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npx tsc --noEmit 2>&1`
Expected: No errors

- [ ] **Step 2: Check existing tests still pass (if any)**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npm test 2>&1 || echo "No tests configured"`
Expected: Pass or "No tests configured"

- [ ] **Step 3: Verify sendEmail is NOT directly exported**

Run: `grep -n "^export.*function sendEmail\|^export async function sendEmail" "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain/src/skills/google/gmail.ts"`
Expected: No output (sendEmail is no longer a direct export)

Run: `grep -n "gmailSendInternal" "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain/src/skills/google/gmail.ts"`
Expected: Shows the `export const gmailSendInternal` line

- [ ] **Step 4: Verify no confirm_send tool exists**

Run: `grep -rn "confirm_send" "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain/src/"`
Expected: No matches (no tool for the LLM to call)

- [ ] **Step 5: Verify draft rejection logic exists**

Run: `grep -n "checkDocNotDraft\|Cannot send a draft" "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain/src/skills/pending-sends.ts"`
Expected: Shows the draft validation function and error message

- [ ] **Step 6: Review all changes**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && git diff HEAD~8 --stat`
Expected: Shows 8 files changed (pending-sends.ts new, gmail.ts, blog.ts, holded.ts, tools.ts, telegram.ts, discord.ts, agent.ts modified)

- [ ] **Step 7: Deploy Brain to server for live testing**

Run deployment command per project conventions. Then test via Telegram:
1. Send: "manda un email a test@example.com con asunto Prueba y cuerpo Esto es una prueba"
2. Brain should show the preview with "Reply YES to send, NO to cancel."
3. Reply: "no" — should cancel
4. Repeat step 1, reply "sí" — should send via Gmail
5. Test Holded doc: "envía la factura INV-xxx a client@email.com"
6. Test attachment: "manda la factura INV-xxx por gmail a client@email.com"
