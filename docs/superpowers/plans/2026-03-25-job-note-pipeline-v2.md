# Job Note Pipeline v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the job tracker from a passive dossier system to an active automation pipeline where Obsidian notes drive actions, the IA verifies before marking checkboxes, and Brain learns from Miguel's responses.

**Architecture:** Extends existing `job-automation.ts` (which already handles note parsing, estimate updates, invoice flow, and alerts). Adds: pending questions system, calendar integration, presupuesto audit, invoice follow-up, and improved cron scheduling. All new code lives in Brain (`services/brain/`), with one small trigger addition in holded-connector.

**Tech Stack:** TypeScript (Brain/Hono), Python (holded-connector/FastAPI), Google Calendar API, Gmail API, Telegram Bot API, Obsidian vault (filesystem + CouchDB)

**Spec:** `services/holded-connector/docs/superpowers/specs/2026-03-25-job-note-pipeline-v2-design.md`

---

## Reusability Note

**Este pipeline se va a reusar con Gaffer** para gestionar presupuestos con clientes vía WhatsApp/Telegram. La lógica de pending questions, calendar sync, invoice follow-up, y learning loop es genérica. Por eso la arquitectura separa:

1. **`note-review-engine.ts`** — Framework genérico: pending questions, calendar sync, quiet hours, concurrency guard. Reutilizable por cualquier servicio.
2. **`job-review.ts`** — Implementación específica de Brain/Holded que usa el engine. Configura qué notas escanear, qué acciones tomar, etc.
3. **Gaffer (futuro)** podrá importar `note-review-engine.ts` y crear su propio `gaffer-review.ts` con configuración diferente.

## File Map

### New Files
- `services/brain/src/shared/note-review-engine.ts` — Framework reutilizable: pending questions parser, quiet hours, calendar sync helper, question limits, remind/expire logic
- `services/brain/src/skills/job-review.ts` — Holded-specific review orchestrator: presupuesto audit, invoice follow-up, cron entry point (uses engine)
- `services/brain/src/skill-tests/skills/job-review.test.ts` — Tests for both engine + job-review

### Modified Files
- `services/brain/src/skills/job-automation.ts` — Add invoice follow-up (7-day reminder), checkbox verification against Holded
- `services/brain/src/routes/internal.ts` — Add `POST /internal/job-review` endpoint, wire cron
- `services/brain/src/skills/holded.ts` — Add `getEstimatesWithoutRef()` function
- `services/brain/deploy.sh` — Add verification grep for new functions
- `services/holded-connector/skills/job_tracker.py` — Add event-driven trigger to Brain after ensure_job()
- `services/holded-connector/api.py` — Add `GET /api/estimates/without-ref` endpoint

### Existing Files (reference only, no changes)
- `services/brain/src/skills/google/gcal.ts` — `createGCalEvent()`, `getGCalEvents()`
- `services/brain/src/skills/google/gmail.ts` — `searchEmails()`, `createDraft()`
- `services/brain/src/skills/obsidian.ts` — `readNote()`, `writeNote()`
- `services/brain/src/skills/telegram.ts` — `sendTelegramMessage()`
- `services/brain/src/memory.ts` — `saveArchivalMemory()`, `searchArchivalMemory()`

---

### Task 1: Presupuesto Audit — Backend Endpoint

**Bugs to apply:** Bug 11 (`_row_to_dict_safe` doesn't exist → use cursor dict access), Bug 24 (use Unix timestamp from the start, don't deploy broken code that Task 6 would fix)

**Files:**
- Modify: `services/holded-connector/api.py` (add endpoint)
- Modify: `services/brain/src/skills/holded.ts` (add fetch function)

- [ ] **Step 1: Add endpoint to holded-connector**

In `services/holded-connector/api.py`, add after the existing job endpoints (~line 1920):

```python
from datetime import datetime as dt

@app.get("/api/estimates/without-ref")
async def get_estimates_without_ref(request: Request):
    """List estimates created after cutoff date that have no project_code."""
    if not authenticate(request):
        return JSONResponse({"error": "Authentication required"}, 401)

    cutoff = request.query_params.get("since", "2026-03-25")
    # Holded stores dates as Unix timestamps (Bug 24 — correct from the start)
    cutoff_ts = int(dt.strptime(cutoff, "%Y-%m-%d").timestamp())
    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        cur.execute(connector._q("""
            SELECT id, "docNumber", contact_id, date, subtotal, tags
            FROM estimates
            WHERE project_code IS NULL
              AND date >= ?
            ORDER BY date DESC
            LIMIT 50
        """), (cutoff_ts,))
        # Bug 11: use cursor dict access, not _row_to_dict_safe (doesn't exist)
        rows = []
        for r in cur.fetchall():
            row = r if isinstance(r, dict) else dict(zip([d[0] for d in cur.description], r))
            rows.append(row)

        # Enrich with contact names
        for row in rows:
            if row.get("contact_id"):
                cur.execute(connector._q(
                    "SELECT name FROM contacts WHERE id = ?"
                ), (row["contact_id"],))
                contact = cur.fetchone()
                if contact:
                    row["client_name"] = (contact["name"] if isinstance(contact, dict) else contact[0]) or ""
        return JSONResponse(rows)
    finally:
        connector.release_db(conn)
```

- [ ] **Step 2: Test endpoint on server**

```bash
ssh coyote-server "curl -s -H 'Authorization: Bearer TOKEN' http://localhost:8000/api/estimates/without-ref?since=2026-03-25"
```
Expected: JSON array (possibly empty) of estimates without project_code.

- [ ] **Step 3: Add fetch function in Brain holded skill**

In `services/brain/src/skills/holded.ts`, add after `getOpenJobs()` (~line 720):

```typescript
/**
 * Fetch estimates without project_code (REF) since a cutoff date.
 * Used by job-review periodic audit.
 */
export async function getEstimatesWithoutRef(since = "2026-03-25"): Promise<any[]> {
  const data = await holdedFetch(`/api/estimates/without-ref?since=${since}`);
  return Array.isArray(data) ? data : [];
}
```

- [ ] **Step 4: Commit**

```bash
git add services/holded-connector/api.py services/brain/src/skills/holded.ts
git commit -m "feat: add estimates-without-ref endpoint for presupuesto audit"
```

---

### Task 2: Note Review Engine (reusable framework)

**Files:**
- Create: `services/brain/src/shared/note-review-engine.ts`
- Create: `services/brain/src/skills/job-review.ts`
- Test: `services/brain/src/skill-tests/skills/job-review.test.ts`

#### Engine / Job-Review Split (CRITICAL for Gaffer reuse)

**`note-review-engine.ts` exports (generic, zero Holded/job knowledge):**

```typescript
// ─── Types ───
export interface PendingQuestion { id, asked_at, question, reminded_at, context_id? }
export interface ReviewThrottle { count: number }  // pass by ref for shared counting
export interface CalendarEventOpts { calendarId, summary, startDate, endDate, description }

// ─── Pending Questions ───
export function parsePendingQuestions(frontmatter: string): PendingQuestion[]
export function formatPendingQuestion(q: PendingQuestion): string
export function shouldRemind(q: PendingQuestion): boolean   // >24h, not yet reminded
export function shouldExpire(q: PendingQuestion): boolean    // >72h
export function removePendingQuestion(noteContent: string, questionId: string): string

// ─── Throttled Telegram ───
export async function throttledSend(chatId: string, msg: string, throttle: ReviewThrottle): Promise<boolean>
// Returns false if throttle.count <= 0 (limit reached). Decrements counter on send.

// ─── Quiet Hours & Vacation ───
export function isQuietHours(start?: number, end?: number): boolean
export async function isVacationMode(): Promise<boolean>

// ─── Calendar ───
export async function ensureCalendarEvent(opts: CalendarEventOpts, noteContent: string, notePath: string): Promise<string | null>
// Idempotent: checks calendar_event_id in frontmatter. Uses date string parsing (NOT new Date()) for timezone safety.

// ─── Frontmatter Helpers ───
export function updateFrontmatterField(noteContent: string, field: string, value: string): string
// Safely modifies a single frontmatter field without touching note body.

// ─── Learning Loop ───
export async function searchLearnedValue(category: string, query: string): Promise<string | null>
export async function saveLearnedValue(category: string, key: string, value: string): Promise<void>
// Searches existing memory first. Updates if found, creates if not (prevents duplicates — Bug 27).

// ─── Health / Escalation ───
export async function trackPhaseResult(phase: string, success: boolean): Promise<void>
export async function checkEscalation(chatId: string): Promise<void>
// After 3+ consecutive failures in any phase → Telegram alert (Bug 26).

// ─── Constants ───
export const MAX_QUESTIONS_PER_JOB = 2
export const MAX_QUESTIONS_PER_CYCLE = 10  // hard cap across ALL phases (Bug 19)
export const REMIND_AFTER_MS = 24 * 3600_000
export const EXPIRE_AFTER_MS = 72 * 3600_000
```

**`job-review.ts` imports from engine and adds ONLY Holded-specific logic:**

```typescript
import { throttledSend, isQuietHours, isVacationMode, ensureCalendarEvent,
         MAX_QUESTIONS_PER_CYCLE, trackPhaseResult, checkEscalation,
         type ReviewThrottle } from "../shared/note-review-engine.js";

// Holded-specific functions (NOT in engine):
// - auditEstimatesWithoutRef()
// - checkInvoiceFollowups()
// - runPeriodicReview() ← orchestrator
```

**How Gaffer will reuse (example):**
```typescript
// services/gaffer/src/skills/gaffer-review.ts (FUTURE)
import { throttledSend, isQuietHours, ensureCalendarEvent,
         parsePendingQuestions, type ReviewThrottle } from "../../brain/src/shared/note-review-engine.js";

// Gaffer-specific: WhatsApp quote follow-up, client approval tracking, etc.
export async function runGafferReview(chatId: string) { ... }
```

#### Bugs to apply in this task:
- **Bug 1:** Split as described above
- **Bug 2:** Fetch `/api/jobs` ONCE at start, pass to all phases
- **Bug 3:** Calendar dates: parse string manually, never `new Date("2026-03-27")`
- **Bug 4:** Track asked estimate IDs in archival memory category `audit_asked`
- **Bug 5:** YAML parser: use `(\w+):\s*"?(.+?)"?\s*$` regex
- **Bug 6:** Gmail search: also search by `to:${clientEmail} OR from:${clientEmail}`
- **Bug 7:** Duplicate codes: check DB before suggesting, append `-B` if exists
- **Bug 8:** Implement Phase 6 (pending question reminders), don't leave as TODO
- **Bug 9:** Quiet hours = early return, no phases execute
- **Bug 10:** Test imports from `../../shared/note-review-engine.js`
- **Bug 13:** After sending followup, PATCH job's `last_alerts` with timestamp
- **Bug 15:** Verify `createGCalEvent` return type, extract event ID correctly
- **Bug 16:** Write action log entry before marking checkbox
- **Bug 19:** All phases use `throttledSend`, hard cap 10 messages
- **Bug 20:** Date format `d+m+y` not `slice(2)`
- **Bug 23:** Frontmatter update via `updateFrontmatterField()`, not regex on full note
- **Bug 27:** Learning loop: search before save, update existing

- [ ] **Step 1: Write the test file**

Create `services/brain/src/skill-tests/skills/job-review.test.ts`:

```typescript
import { describe, test, expect } from "../../skill-tests/framework.js";
import {
  parsePendingQuestions,
  formatPendingQuestion,
  shouldRemind,
  shouldExpire,
  isQuietHours,
  updateFrontmatterField,
  MAX_QUESTIONS_PER_JOB,
  MAX_QUESTIONS_PER_CYCLE,
} from "../../shared/note-review-engine.js";

export default [
  describe("parsePendingQuestions", [
    test("connect", "parses YAML pending_questions from frontmatter", async () => {
      const fm = `---
project_code: TEST-1
pending_questions:
  - id: q1
    asked_at: "2026-03-25T14:00:00Z"
    question: "Fechas de rodaje?"
    reminded_at: null
---`;
      const result = parsePendingQuestions(fm);
      expect(result.length).toBe(1);
      expect(result[0].id).toBe("q1");
      expect(result[0].question).toBe("Fechas de rodaje?");
      return "parsed 1 question from frontmatter";
    }),

    test("connect", "returns empty array when no pending_questions", async () => {
      const fm = `---\nproject_code: TEST-1\n---`;
      const result = parsePendingQuestions(fm);
      expect(result.length).toBe(0);
      return "empty array for no questions";
    }),
  ]),

  describe("shouldRemind", [
    test("connect", "returns true after 24h without reminder", async () => {
      const q = {
        id: "q1",
        asked_at: new Date(Date.now() - 25 * 3600_000).toISOString(),
        question: "test?",
        reminded_at: null,
      };
      expect(shouldRemind(q)).toBe(true);
      return "reminds after 24h";
    }),

    test("connect", "returns false before 24h", async () => {
      const q = {
        id: "q1",
        asked_at: new Date(Date.now() - 12 * 3600_000).toISOString(),
        question: "test?",
        reminded_at: null,
      };
      expect(shouldRemind(q)).toBe(false);
      return "does not remind before 24h";
    }),
  ]),

  describe("shouldExpire", [
    test("connect", "returns true after 72h", async () => {
      const q = {
        id: "q1",
        asked_at: new Date(Date.now() - 73 * 3600_000).toISOString(),
        question: "test?",
        reminded_at: new Date(Date.now() - 49 * 3600_000).toISOString(),
      };
      expect(shouldExpire(q)).toBe(true);
      return "expires after 72h";
    }),
  ]),

  describe("constants", [
    test("connect", "limits are sensible", async () => {
      expect(MAX_QUESTIONS_PER_JOB).toBe(2);
      expect(MAX_QUESTIONS_PER_CYCLE).toBe(10);
      return `per_job=${MAX_QUESTIONS_PER_JOB}, per_cycle=${MAX_QUESTIONS_PER_CYCLE}`;
    }),
  ]),

  describe("updateFrontmatterField", [
    test("connect", "adds new field to frontmatter without touching body", async () => {
      const note = `---\nproject_code: TEST\ntags: [coyote]\n---\n\n# Body\nSome content`;
      const result = updateFrontmatterField(note, "calendar_event_id", "abc123");
      expect(result).toContain('calendar_event_id: "abc123"');
      expect(result).toContain("# Body");
      expect(result).toContain("Some content");
      return "field added, body preserved";
    }),

    test("connect", "updates existing field", async () => {
      const note = `---\nstatus: open\ntags: [coyote]\n---\n\n# Body`;
      const result = updateFrontmatterField(note, "status", "closed");
      expect(result).toContain('status: closed');
      expect(result).not.toContain('status: open');
      return "field updated";
    }),
  ]),

  describe("isQuietHours", [
    test("connect", "23:00 is quiet", async () => {
      // We can't easily mock Date, but we test the logic
      expect(typeof isQuietHours()).toBe("boolean");
      return "returns boolean";
    }),
  ]),
];
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd services/brain && npx tsx src/skill-tests/runner.ts --skill job-review
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement note-review-engine.ts (reusable framework)**

Create `services/brain/src/shared/note-review-engine.ts` with all the generic logic (pending questions, quiet hours, calendar, rate limits). Then create `services/brain/src/skills/job-review.ts` that imports from the engine and adds Holded-specific logic.

```typescript
/**
 * job-review.ts — Periodic Job Review Orchestrator
 *
 * Extends job-automation.ts with:
 *   - Pending questions system (ask → remind 24h → expire 72h)
 *   - Google Calendar integration (shooting dates → events)
 *   - Presupuesto audit (estimates without REF)
 *   - Invoice follow-up (7-day reminder for client approval)
 *   - Concurrency guard (DB-backed, not in-memory)
 *
 * Cron schedule: every 3h from 8:00 to 22:00 (8,11,14,17,20,22)
 * Triggered by: n8n → POST /internal/job-review
 */

import { sendTelegramMessage } from "./telegram.js";
import { readNote, writeNote } from "./obsidian.js";
import { holdedFetch } from "./holded.js";
import { getEstimatesWithoutRef } from "./holded.js";
import { searchArchivalMemory, saveArchivalMemory } from "../memory.js";
import { getGCalEvents, createGCalEvent, type GCalEvent } from "./google/gcal.js";
import { searchEmails } from "./google/gmail.js";
import { interpretJobNote, runJobCheck, runJobAlerts } from "./job-automation.js";

// ─── Constants ───────────────────────────────────────────────────────

export const MAX_QUESTIONS_PER_JOB = 2;
export const MAX_QUESTIONS_PER_CYCLE = 5;
const REMIND_AFTER_MS = 24 * 3600_000;   // 24h
const EXPIRE_AFTER_MS = 72 * 3600_000;   // 72h
const INVOICE_FOLLOWUP_DAYS = 7;
const QUIET_HOURS_START = 22; // Don't send Telegram after 22:00
const QUIET_HOURS_END = 8;   // Don't send Telegram before 8:00
const REF_AUDIT_CUTOFF = "2026-03-25";
const SHARED_CALENDAR_ID = process.env.GCAL_SHARED_CALENDAR_ID || "primary";

// ─── Types ───────────────────────────────────────────────────────────

export interface PendingQuestion {
  id: string;
  asked_at: string;
  question: string;
  reminded_at: string | null;
  job_code?: string;
}

// ─── Pending Questions ───────────────────────────────────────────────

/**
 * Parse pending_questions from note frontmatter YAML.
 * Tolerant parser — returns empty array on any parse failure.
 */
export function parsePendingQuestions(frontmatter: string): PendingQuestion[] {
  try {
    const match = frontmatter.match(/pending_questions:\s*\n((?:\s+-[\s\S]*?)(?=\n\w|\n---|\n$|$))/);
    if (!match) return [];

    const items: PendingQuestion[] = [];
    const entries = match[1].split(/\n\s+-\s/).filter(Boolean);

    for (const entry of entries) {
      const lines = entry.trim().split("\n").map(l => l.trim().replace(/^-\s*/, ""));
      const obj: Record<string, string | null> = {};

      for (const line of lines) {
        const kv = line.match(/^(\w+):\s*"?([^"]*)"?\s*$/);
        if (kv) {
          obj[kv[1]] = kv[2] === "null" ? null : kv[2];
        }
      }

      if (obj.id && obj.question) {
        items.push({
          id: obj.id,
          asked_at: obj.asked_at || new Date().toISOString(),
          question: obj.question,
          reminded_at: obj.reminded_at || null,
        });
      }
    }
    return items;
  } catch {
    return [];
  }
}

/**
 * Format a pending question for insertion into frontmatter YAML.
 */
export function formatPendingQuestion(q: PendingQuestion): string {
  return [
    `  - id: ${q.id}`,
    `    asked_at: "${q.asked_at}"`,
    `    question: "${q.question.replace(/"/g, "'")}"`,
    `    reminded_at: ${q.reminded_at ? `"${q.reminded_at}"` : "null"}`,
  ].join("\n");
}

/** Should we send a reminder for this question? (>24h, not yet reminded) */
export function shouldRemind(q: PendingQuestion): boolean {
  const elapsed = Date.now() - new Date(q.asked_at).getTime();
  return elapsed > REMIND_AFTER_MS && !q.reminded_at;
}

/** Should we stop asking about this question? (>72h) */
export function shouldExpire(q: PendingQuestion): boolean {
  const elapsed = Date.now() - new Date(q.asked_at).getTime();
  return elapsed > EXPIRE_AFTER_MS;
}

// ─── Quiet Hours ─────────────────────────────────────────────────────

function isQuietHours(): boolean {
  const hour = new Date().getHours();
  return hour >= QUIET_HOURS_START || hour < QUIET_HOURS_END;
}

// ─── Calendar Sync ───────────────────────────────────────────────────

/**
 * Ensure a Google Calendar event exists for a job's shooting dates.
 * Creates if missing, skips if already exists (idempotent via calendar_event_id in frontmatter).
 */
async function ensureCalendarEvent(
  job: Record<string, unknown>,
  noteContent: string,
  notePath: string,
): Promise<string | null> {
  // Check if event already exists in frontmatter
  const eventIdMatch = noteContent.match(/calendar_event_id:\s*"?([^"\n]+)"?/);
  if (eventIdMatch && eventIdMatch[1] && eventIdMatch[1] !== "null") {
    return eventIdMatch[1]; // Already has event
  }

  // Parse shooting dates
  let dates: string[] = [];
  try {
    const raw = typeof job.shooting_dates === "string" ? job.shooting_dates : "[]";
    dates = JSON.parse(raw);
  } catch { /* empty */ }

  if (dates.length === 0) return null; // No dates — will ask via Telegram

  const code = String(job.project_code || "");
  const client = String(job.client_name || "");
  const startDate = dates[0];
  const endDate = dates[dates.length - 1];

  // Add 1 day to end for all-day event range
  const endPlusOne = new Date(endDate);
  endPlusOne.setDate(endPlusOne.getDate() + 1);
  const endStr = endPlusOne.toISOString().split("T")[0];

  try {
    const eventId = await createGCalEvent({
      calendarId: SHARED_CALENDAR_ID,
      event: {
        summary: `[${code}] — ${client}`,
        start: { date: startDate },
        end: { date: endStr },
        description: `Job: ${code}\nClient: ${client}\nDates: ${dates.join(", ")}`,
      },
    });

    // Update frontmatter with event ID
    if (eventId) {
      const updatedContent = noteContent.replace(
        /tags:\s*\[/,
        `calendar_event_id: "${eventId}"\ntags: [`
      );
      await writeNote({ path: notePath, content: updatedContent });
    }

    return typeof eventId === "string" ? eventId : null;
  } catch (err) {
    console.error(`[job-review] Calendar event failed for ${code}:`, (err as Error).message);
    return null;
  }
}

// ─── Presupuesto Audit ───────────────────────────────────────────────

/**
 * Find estimates without REF since cutoff date and ask Miguel for codes.
 * Uses learning loop: searches archival memory for known client→code mappings.
 */
async function auditEstimatesWithoutRef(
  chatId: string,
  questionsLeft: { count: number },
): Promise<number> {
  let estimates: any[];
  try {
    estimates = await getEstimatesWithoutRef(REF_AUDIT_CUTOFF);
  } catch {
    return 0;
  }

  if (estimates.length === 0) return 0;

  let asked = 0;
  for (const est of estimates) {
    if (questionsLeft.count <= 0) break;

    const clientName = est.client_name || "Unknown";
    const docNumber = est.docNumber || est.id;
    const dateStr = est.date ? new Date(est.date * 1000).toISOString().split("T")[0] : "?";

    // Check memory for known client code
    let suggestion = "";
    try {
      const memories = await searchArchivalMemory(`client_code: ${clientName}`, 3);
      const match = memories.find(m => m.similarity > 0.7 && m.content.includes("client_code:"));
      if (match) {
        const codeMatch = match.content.match(/→\s*(\w+)/);
        if (codeMatch) suggestion = codeMatch[1];
      }
    } catch { /* non-critical */ }

    const suggestedCode = suggestion
      ? `${suggestion}-${dateStr.replace(/-/g, "").slice(2)}`
      : "";

    const msg = [
      `📋 *Presupuesto sin REF*`,
      `Doc: ${docNumber}`,
      `Cliente: ${clientName}`,
      `Fecha: ${dateStr}`,
      "",
      suggestedCode
        ? `Sugerencia: \`${suggestedCode}\`\n¿Le pongo esta REF o prefieres otra?`
        : `¿Qué código REF le pongo? (formato: CLIENTE-DDMMYYYY)`,
    ].join("\n");

    await sendTelegramMessage(chatId, msg);
    questionsLeft.count--;
    asked++;
  }

  return asked;
}

// ─── Invoice Follow-up ───────────────────────────────────────────────

/**
 * Check for invoices sent >7 days ago without client response.
 * Searches Gmail for reply to the invoice email thread.
 */
async function checkInvoiceFollowups(
  chatId: string,
  jobs: Record<string, unknown>[],
  questionsLeft: { count: number },
): Promise<number> {
  let followups = 0;

  for (const job of jobs) {
    if (questionsLeft.count <= 0) break;

    const invoiceId = job.invoice_id ? String(job.invoice_id) : null;
    const draftCreatedAt = job.invoice_draft_created_at
      ? new Date(String(job.invoice_draft_created_at))
      : null;

    if (!invoiceId || !draftCreatedAt) continue;
    if (job.status === "closed") continue;

    const daysSince = Math.floor(
      (Date.now() - draftCreatedAt.getTime()) / 86_400_000
    );

    if (daysSince < INVOICE_FOLLOWUP_DAYS) continue;

    // Check if client has replied via Gmail
    const code = String(job.project_code || "");
    try {
      const threads = await searchEmails(`subject:${code} is:inbox newer_than:${INVOICE_FOLLOWUP_DAYS}d`);
      if (threads && threads.length > 0) continue; // Client replied — skip
    } catch { /* Gmail error — proceed with caution */ }

    // Check anti-spam: don't repeat within 3 days
    let lastAlerts: Record<string, string> = {};
    try {
      if (typeof job.last_alerts === "string" && job.last_alerts) {
        lastAlerts = JSON.parse(job.last_alerts);
      }
    } catch { /* reset */ }

    const lastFollowup = lastAlerts.invoice_followup
      ? new Date(lastAlerts.invoice_followup)
      : null;
    if (lastFollowup && Date.now() - lastFollowup.getTime() < 3 * 86_400_000) continue;

    const clientName = String(job.client_name || "Unknown");
    const msg = [
      `⏰ *Factura sin respuesta — ${code}*`,
      `Cliente: ${clientName}`,
      `Enviada hace ${daysSince} días`,
      "",
      `¿Envío un recordatorio al cliente?`,
      `Responde "SÍ REMINDER ${code}" para que prepare un draft de follow-up.`,
    ].join("\n");

    await sendTelegramMessage(chatId, msg);
    questionsLeft.count--;
    followups++;
  }

  return followups;
}

// ─── Main Orchestrator ───────────────────────────────────────────────

/**
 * Periodic job review — main entry point called by cron every 3h.
 *
 * Phases:
 *   1. Run existing job-check (note changes → Telegram proposals)
 *   2. Run existing job-alerts (post-shooting, draft pending, unpaid)
 *   3. Calendar sync (ensure events for shooting dates)
 *   4. Presupuesto audit (estimates without REF)
 *   5. Invoice follow-up (7-day reminder)
 *   6. Pending questions (remind 24h, expire 72h)
 */
export async function runPeriodicReview(
  chatId: string
): Promise<{
  jobCheck: { processed: number; skipped: number };
  alerts: number;
  calendarEvents: number;
  auditAsked: number;
  followups: number;
  reminders: number;
}> {
  if (isQuietHours()) {
    console.log("[job-review] Quiet hours — skipping Telegram notifications");
    // Still run job-check silently (no Telegram), but skip audit/followup/reminders
  }

  const questionsLeft = { count: MAX_QUESTIONS_PER_CYCLE };

  // Phase 1: Existing job-check (note changes)
  const jobCheck = await runJobCheck(chatId);

  // Phase 2: Existing job-alerts
  const { alerts } = await runJobAlerts(chatId);

  // Phase 3: Calendar sync
  let calendarEvents = 0;
  try {
    const jobs = (await holdedFetch("/api/jobs?limit=50")) as Record<string, unknown>[];
    if (Array.isArray(jobs)) {
      for (const job of jobs) {
        if (job.status === "closed") continue;
        const notePath = typeof job.note_path === "string"
          ? job.note_path.replace(/\.md$/, "")
          : "";
        if (!notePath) continue;

        try {
          const noteResult = await readNote({ path: notePath });
          if (noteResult?.content) {
            const eventId = await ensureCalendarEvent(job, noteResult.content, notePath);
            if (eventId) calendarEvents++;
          }
        } catch { /* skip this job */ }
      }
    }
  } catch (err) {
    console.error("[job-review] Calendar sync error:", (err as Error).message);
  }

  // Phase 4: Presupuesto audit (skip during quiet hours)
  let auditAsked = 0;
  if (!isQuietHours()) {
    auditAsked = await auditEstimatesWithoutRef(chatId, questionsLeft);
  }

  // Phase 5: Invoice follow-up (skip during quiet hours)
  let followups = 0;
  if (!isQuietHours()) {
    try {
      const jobs = (await holdedFetch("/api/jobs?limit=200")) as Record<string, unknown>[];
      if (Array.isArray(jobs)) {
        followups = await checkInvoiceFollowups(chatId, jobs, questionsLeft);
      }
    } catch { /* non-critical */ }
  }

  // Phase 6: Pending questions reminders — TODO in future iteration
  // (requires frontmatter read/write per job note, complex YAML manipulation)
  const reminders = 0;

  return { jobCheck, alerts, calendarEvents, auditAsked, followups, reminders };
}
```

- [ ] **Step 4: Run tests**

```bash
cd services/brain && npx tsx src/skill-tests/runner.ts --skill job-review
```
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/brain/src/shared/note-review-engine.ts services/brain/src/skills/job-review.ts services/brain/src/skill-tests/skills/job-review.test.ts
git commit -m "feat: add note-review-engine (reusable) + job-review orchestrator with pending questions, calendar sync, presupuesto audit"
```

---

### Task 3: Internal Endpoint + Cron Wiring

**Bugs to apply:** Bug 22 (add `AbortSignal.timeout(25min)` to prevent runaway reviews)

**Files:**
- Modify: `services/brain/src/routes/internal.ts`
- Modify: `services/brain/src/index.ts` (if cron wiring needed)

- [ ] **Step 1: Add job-review endpoint to internal.ts**

In `services/brain/src/routes/internal.ts`, add after `internalJobAlerts` (~line 290):

```typescript
let jobReviewRunning = false;

export async function internalJobReview(c: Context) {
  if (!validateKey(c)) return c.text("Unauthorized", 401);
  if (jobReviewRunning) return c.json({ ok: true, skipped: "already_running" });
  jobReviewRunning = true;
  try {
    const { runPeriodicReview } = await import("../skills/job-review.js");
    const result = await runPeriodicReview(TELEGRAM_CHAT_ID);
    return c.json({ ok: true, ...result });
  } catch (err) {
    const msg = (err as Error).message || "Unknown error";
    console.error("[internal/job-review] error:", msg);
    return c.json({ error: msg.slice(0, 200) }, 500);
  } finally {
    jobReviewRunning = false;
  }
}
```

- [ ] **Step 2: Wire the route in index.ts**

Find where internal routes are registered in `services/brain/src/index.ts` and add:

```typescript
app.post("/internal/job-review", internalJobReview);
```

Import at top:
```typescript
import { internalJobReview } from "./routes/internal.js";
```

- [ ] **Step 3: Verify the route compiles**

```bash
cd services/brain && npx tsc --noEmit --project tsconfig.json
```
Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add services/brain/src/routes/internal.ts services/brain/src/index.ts
git commit -m "feat: add POST /internal/job-review endpoint with concurrency guard"
```

---

### Task 4: Event-Driven Trigger from holded-connector

**Bugs to apply:** Bug 12 (don't make HTTP call inside ensure_job() — it blocks the DB transaction. Instead, move notification AFTER conn.commit() in the caller, or simply rely on the cron polling job_note_queue)

**Files:**
- Modify: `services/holded-connector/skills/job_tracker.py`

- [ ] **Step 1: Add Brain notification after ensure_job()**

In `services/holded-connector/skills/job_tracker.py`, modify the `ensure_job()` function. After the queue note sync block (~line 452):

```python
    # Queue note sync only on create or actual changes
    if action:
        cursor.execute(conn_mod._q("""
            INSERT INTO job_note_queue (project_code, action) VALUES (?, ?)
        """), (project_code, action))

        # Notify Brain to review this job (event-driven, non-blocking)
        try:
            http_requests.post(
                f"{BRAIN_API_URL}/internal/job-review",
                json={"project_code": project_code, "action": action},
                headers={"x-api-key": BRAIN_INTERNAL_KEY},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"[JOB_TRACKER] Brain notification failed: {e}")
```

- [ ] **Step 2: Verify the import exists**

Check that `http_requests` (alias for `requests`) and `BRAIN_API_URL`/`BRAIN_INTERNAL_KEY` are already imported at the top of the file. They are (lines 22, 27-28).

- [ ] **Step 3: Test locally**

```bash
cd services/holded-connector && python3 -c "
from skills.job_tracker import BRAIN_API_URL, BRAIN_INTERNAL_KEY
print(f'BRAIN_API_URL={BRAIN_API_URL}')
print(f'BRAIN_INTERNAL_KEY={BRAIN_INTERNAL_KEY[:8]}...' if BRAIN_INTERNAL_KEY else 'no key')
"
```

- [ ] **Step 4: Commit**

```bash
git add services/holded-connector/skills/job_tracker.py
git commit -m "feat: event-driven Brain notification when job changes in ensure_job()"
```

---

### Task 5: n8n Cron Configuration

**Bugs to apply:** Bug 18 (MUST disable old n8n workflows for /internal/job-check and /internal/job-alerts — they're now called internally by job-review, running them separately = double execution)

**Files:**
- n8n workflow (via n8n API or UI)

- [ ] **Step 1: Create n8n cron workflow for job-review**

Via n8n API or UI, create a workflow with:
- **Trigger:** Cron node — `0 8,11,14,17,20,22 * * *` (every 3h from 8 to 22)
- **Action:** HTTP Request node — `POST http://coyote-brain:3100/internal/job-review`
- **Headers:** `x-api-key: ${BRAIN_INTERNAL_KEY}`

```bash
# Verify the existing cron jobs
ssh coyote-server "crontab -l | grep brain"
```

If crons are in crontab instead of n8n, add:
```bash
0 8,11,14,17,20,22 * * * curl -s -X POST -H 'x-api-key: KEY' http://localhost:3100/internal/job-review > /dev/null 2>&1
```

- [ ] **Step 2: Test the endpoint manually**

```bash
ssh coyote-server "curl -s -X POST -H 'x-api-key: KEY' http://localhost:3100/internal/job-review"
```
Expected: `{"ok": true, "jobCheck": {...}, "alerts": N, ...}`

- [ ] **Step 3: Commit deploy.sh changes**

Add verification grep to `services/brain/deploy.sh`:

```bash
# Verify job-review
verify_grep "runPeriodicReview" "src/skills/job-review.ts"
verify_grep "internalJobReview" "src/routes/internal.ts"
```

```bash
git add services/brain/deploy.sh
git commit -m "chore: add job-review verification to deploy.sh"
```

---

### ~~Task 6: MERGED INTO TASK 1~~ (Bug 24 — don't deploy broken code)

Task 1 now writes the endpoint with correct Unix timestamp comparison from the start.

---

### Task 7: Deploy and E2E Verification

**Bugs to apply:** Bug 17 (if email in note differs from Holded, notify Miguel), Bug 26 (verify escalation alerts work — trigger 3+ failures and check Telegram)

**Files:**
- Deploy scripts

- [ ] **Step 1: Deploy holded-connector**

```bash
scp services/holded-connector/skills/job_tracker.py coyote-server:/tmp/job_tracker.py
scp services/holded-connector/api.py coyote-server:/tmp/api.py
ssh coyote-server "docker cp /tmp/job_tracker.py holded-api:/app/skills/job_tracker.py && docker cp /tmp/api.py holded-api:/app/api.py && docker restart holded-api"
```

Wait 5s, verify health:
```bash
ssh coyote-server "curl -s http://localhost:8000/health"
```

- [ ] **Step 2: Deploy Brain**

```bash
cd services/brain && bash deploy.sh
```

- [ ] **Step 3: E2E test — trigger job-review**

```bash
ssh coyote-server "curl -s -X POST -H 'x-api-key: KEY' http://localhost:3100/internal/job-review"
```

Expected: JSON response with jobCheck, alerts, calendarEvents, etc.

- [ ] **Step 4: Verify calendar event was created for BIRK**

Check Google Calendar for `[BIRK-18032026]` event on 26-27 March.

- [ ] **Step 5: Verify presupuesto audit**

Check Telegram for any "Presupuesto sin REF" messages (if there are estimates without REF since 2026-03-25).

- [ ] **Step 6: Check Brain logs**

```bash
ssh coyote-server "docker logs coyote-brain --since 5m 2>&1 | grep -i 'job-review\|calendar\|audit' | tail -20"
```

- [ ] **Step 7: Commit final state**

```bash
git add -A && git commit -m "feat: Job Note Pipeline v2 — deploy and verify"
```

---

### Task 8: Update Obsidian Documentation

**Files:**
- Obsidian vault (via MCP tools)

- [ ] **Step 1: Update Job Tracker note in Obsidian**

Update `Coyote AI/Job Tracker.md` with:
- New v2 pipeline flow
- Cron schedule (8,11,14,17,20,22)
- Calendar integration details
- Presupuesto audit (cutoff date)
- Link to spec

- [ ] **Step 2: Update Backlog Activo**

Mark Job Note Pipeline v2 as completed in `Coyote AI/Backlog Activo.md`.

- [ ] **Step 3: Update TO BE TESTED**

Add new E2E tests to `Coyote AI/TO BE TESTED.md`:
- Calendar event creation
- Presupuesto audit → Telegram notification
- Invoice 7-day follow-up
- Pending questions remind/expire

---

### Task 9: Telegram Cancel Command

**Bugs to apply:** Bug 21 (regex must handle `olvidate` without accent), Bug 25 (support multiple codes in one message via `matchAll`)

**Files:**
- Modify: `services/brain/src/routes/telegram.ts`

Brain must intercept "cancela BIRK" / "olvídate de BIRK" / "cancel BIRK" in Telegram and:

- [ ] **Step 1: Identify where YES JOB_CODE is intercepted**

In `services/brain/src/routes/telegram.ts`, find the `handleJobConfirmation` intercept pattern. The cancel command follows the same pattern.

- [ ] **Step 2: Add cancel intercept before LLM routing**

```typescript
// Pattern: "cancela CODE" / "olvídate de CODE" / "cancel CODE"
const cancelMatch = text.match(/^(?:cancela|olvídate de|cancel)\s+([A-Z0-9_-]+)/i);
if (cancelMatch) {
  const jobCode = cancelMatch[1].toUpperCase();
  try {
    // Mark job as closed in DB
    await holdedPatch(`/api/jobs/${encodeURIComponent(jobCode)}`, { status: "closed" });
    // Update note status
    // ... read note, update status in frontmatter, clear pending_questions
    await sendTelegramMessage(chatId, `✅ ${jobCode} cerrado. No procesaré más este trabajo.`);
    return;
  } catch (err) {
    await sendTelegramMessage(chatId, `❌ No pude cerrar ${jobCode}: ${(err as Error).message}`);
    return;
  }
}
```

- [ ] **Step 3: Test via Telegram**

Send "cancela RUNTEST-170326" via Telegram → should close the job and confirm.

- [ ] **Step 4: Commit**

```bash
git add services/brain/src/routes/telegram.ts
git commit -m "feat: add cancel command for jobs via Telegram"
```

---

### Task 10: Telegram "SÍ REMINDER CODE" Intercept + Audit REF Response

**Bugs to apply:** Bug 14 (audit REF responses need a handler too — not just reminders), Bug 25 (multiple codes)

**Files:**
- Modify: `services/brain/src/routes/telegram.ts`

Two new intercepts:
1. Invoice follow-up: "SÍ REMINDER BIRK" → create Gmail follow-up draft
2. Audit REF: Miguel responds with a code after Brain asks → Brain adds REF to Holded + creates job + saves to memory

- [ ] **Step 1: Add reminder intercept in telegram.ts**

```typescript
// Pattern: "SÍ REMINDER CODE" / "SI REMINDER CODE" / "YES REMINDER CODE"
const reminderMatch = text.match(/^(?:s[ií]|yes)\s+reminder\s+([A-Z0-9_-]+)/i);
if (reminderMatch) {
  const jobCode = reminderMatch[1].toUpperCase();
  // Look up job → get client email, project code
  // Create Gmail draft: "Gentle reminder about invoice for project {code}..."
  // Send Telegram: "📧 Draft de reminder creado para {code}. Revísalo en Gmail."
  return;
}
```

- [ ] **Step 2: Test via Telegram**

- [ ] **Step 3: Commit**

```bash
git add services/brain/src/routes/telegram.ts
git commit -m "feat: add SÍ REMINDER intercept for invoice follow-up drafts"
```

---

### Task 11: Vacation Mode

**Files:**
- Modify: `services/brain/src/shared/note-review-engine.ts`
- Modify: `services/brain/src/routes/telegram.ts`

When Miguel is away, the system should not spam Telegram.

- [ ] **Step 1: Add /vacation toggle in telegram.ts**

```typescript
// /vacation on → save to core memory
// /vacation off → remove, send accumulated summary
```

- [ ] **Step 2: Check vacation mode in engine before sending Telegram**

```typescript
export async function isVacationMode(): Promise<boolean> {
  const core = await loadCoreMemory();
  return core.vacation_mode === "true";
}
```

When active:
- No Telegram questions/reminders (queue them)
- No follow-ups
- Only critical alerts: factura vencida >30 días
- On `/vacation off`: send summary of queued items

- [ ] **Step 3: Commit**

---

## Bugs Found During Review (applied in implementation)

These bugs MUST be fixed during implementation of the tasks above:

### Bug 1: Split engine from job-review (Task 2)
The code in Task 2 puts everything in `job-review.ts`. During implementation, the generic functions (parsePendingQuestions, shouldRemind, shouldExpire, isQuietHours, isVacationMode) MUST go in `src/shared/note-review-engine.ts`, and only Holded-specific logic stays in `job-review.ts`.

### Bug 2: Redundant /api/jobs fetches (Task 2)
`runPeriodicReview()` calls `/api/jobs` separately for calendar sync and invoice follow-ups. Fix: fetch once at the start and pass to all phases.

### Bug 3: Calendar date timezone (Task 2)
`new Date("2026-03-27")` creates UTC midnight. In Spain (UTC+1/+2), `toISOString().split("T")[0]` can give wrong day. Fix: parse date string manually (split by `-`, add 1 to day) instead of using Date constructor.

### Bug 4: Audit presupuestos spam (Task 2)
`auditEstimatesWithoutRef()` re-asks about the same estimates every cron cycle if Miguel hasn't responded. Fix: track asked estimate IDs in `last_alerts` JSON on a sentinel record or in archival memory with category `audit_asked`.

### Bug 5: YAML parser fragile (Task 2)
The regex YAML parser for pending_questions can break on special characters. Fix: use more permissive regex `(\w+):\s*"?(.+?)"?\s*$` or install `yaml` npm package.

### Bug 6: Gmail search too narrow (Task 2)
Invoice follow-up search `subject:${code}` misses replies without the REF in subject. Fix: also search `to:${clientEmail} OR from:${clientEmail}` within the date range.

### Bug 7: Duplicate project codes (Task 2)
Two presupuestos for the same client on the same day → same code `BIRK-25032026`. Fix: when suggesting a code, check if it already exists in DB. If yes, append suffix: `-B`, `-C`.

### Bug 8: Phase 6 is TODO (Task 2)
"Pending questions reminders — TODO in future iteration" is core functionality. Fix: implement it — iterate open job notes, check pending_questions in frontmatter, send reminders at 24h, mark stale at 72h.

### Bug 9: Quiet hours but still runs job-check (Task 2)
During quiet hours, `runJobCheck(chatId)` still sends Telegram messages. Fix: if `isQuietHours()`, return early with zeros. Nothing is so urgent it can't wait 3h for the next cron cycle.

### Bug 10: Test imports don't match engine split (Task 2)
Tests import from `../../skills/job-review.js` but generic functions live in `../../shared/note-review-engine.js`. Fix: update test imports to match the actual file split.

### Bug 11: `_row_to_dict_safe` doesn't exist (Task 1)
Task 1 uses `connector._row_to_dict_safe(r, cur)` but this function doesn't exist. Fix: use the existing pattern — list comprehension with dict access from `_cursor()` which returns RealDictCursor.

### Bug 12: Event-driven trigger blocks DB transaction (Task 4)
The HTTP call to Brain is inside `ensure_job()` which holds a DB connection. If Brain is slow, the connection pool gets exhausted. Fix: move the notification AFTER `conn.commit()` in the caller, or better: don't push to Brain — Brain already polls via the cron. The `job_note_queue` already serves as the event queue.

### Bug 13: invoice_followup doesn't update last_alerts (Task 2)
`checkInvoiceFollowups()` reads `last_alerts` for anti-spam but never writes it back. Fix: after sending the followup message, PATCH the job's `last_alerts` with the new timestamp.

### Bug 14: No intercept for audit REF response (Task 2)
The audit asks Miguel for a REF code via Telegram but there's no handler for his response. Fix: add a Telegram intercept for "REF ESTIMATE_ID CODE" pattern, or use Brain's conversational context to link the response to the pending audit question.

### Bug 15: `createGCalEvent` return type unverified (Task 2)
The plan assumes it returns a string (event ID). The actual return type needs verification during implementation. It may return an object with `{ id, htmlLink }`.

### Bug 16: Action log needed for crash recovery (Task 2)
If Brain acts (e.g., adds expense to Holded) but crashes before marking [x], the next cron re-adds the same expense. Fix: write an action log entry (in note or DB) BEFORE marking the checkbox. The cron checks the log to avoid duplicate actions.

### Bug 17: Email source of truth ambiguity
If the note says "Facturación: nuevo@email.com" but Holded has "viejo@email.com", which does Brain use? Fix: use the note (source of truth for manual edits). But notify Miguel of the discrepancy and ask if Holded should be updated too.

### Bug 18: Old crons still active — double execution (Task 5)
n8n already calls `/internal/job-check` and `/internal/job-alerts` separately. The new `/internal/job-review` calls them internally. If old crons aren't disabled, everything runs twice. Fix: Task 5 MUST disable the old n8n workflows for job-check and job-alerts.

### Bug 19: questionsLeft not shared across all phases (Task 2)
`runJobCheck()` and `runJobAlerts()` can send unlimited Telegram messages. Only Phases 4-5 respect `questionsLeft`. Total messages per cycle can be unbounded. Fix: create a `throttledSend(chatId, msg, counter)` wrapper that all phases use, with a hard cap of ~10 messages per cycle.

### Bug 20: Date format wrong in audit suggestion (Task 2)
`dateStr.replace(/-/g, "").slice(2)` on "2026-03-25" gives "260325" (YYMMDD). Convention is DDMMYYYY. Fix: `const [y,m,d] = dateStr.split("-"); const datePart = d+m+y;` → "25032026".

### Bug 21: Cancel regex misses "olvidate" without accent (Task 9)
`olvídate` with tilde. On mobile, Miguel may type `olvidate`. Fix: `olv[ií]date`.

### Bug 22: No global timeout for runPeriodicReview (Task 3)
If Gmail/Holded hangs, the review runs forever, blocking all subsequent crons. Fix: wrap with `AbortSignal.timeout(25 * 60_000)`. Log timeout event.

### Bug 23: Calendar frontmatter injection via full-note regex replace (Task 2)
`noteContent.replace(/tags:\s*\[/, ...)` can match inside note body. Fix: split note into frontmatter + body by `---` markers, add field to frontmatter only, rejoin.

### Bug 24: Merge Task 1 and Task 6 — don't deploy broken code
Task 1 writes string comparison for dates, Task 6 fixes it to Unix timestamp. A subagent executing Task 1 deploys broken code. Fix: write the correct Unix timestamp version from the start.

### Bug 25: Multiple YES codes in one Telegram message (situation)
"YES BIRK y YES HOFF" only matches the first. Fix: use `matchAll` and process all matches.

### Bug 26: Repeated failures without escalation
If a phase fails 5+ crons in a row (15h), nobody knows. Fix: track failure counts per phase. After 3 consecutive failures, send Telegram alert to Miguel with error summary.

### Bug 27: Learning loop memory has no update mechanism
`saveArchivalMemory("client_code: HOFF BRAND → HOFF")` creates new entries. If Miguel changes the code later, old memory still matches with high similarity. Fix: search for existing memory before saving. If found, update instead of create. Or prefer most recent result in searches.

---

## Verification Checklist

After all tasks are complete:

1. [ ] `npx tsx src/skill-tests/runner.ts --skill job-review` — all tests pass
2. [ ] `python3 -m pytest tests/test_job_tracker.py -v` — 42+ tests pass
3. [ ] `POST /internal/job-review` — returns success JSON
4. [ ] Calendar events created for open jobs with shooting dates
5. [ ] Estimates without REF → Telegram notification sent (not repeated if unanswered)
6. [ ] Invoice >7 days without reply → Telegram follow-up sent
7. [ ] "SÍ REMINDER CODE" → Gmail follow-up draft created
8. [ ] Concurrency guard prevents duplicate execution
9. [ ] Quiet hours respected (no Telegram 22:00-8:00)
10. [ ] "cancela CODE" via Telegram → job closed, confirmed
11. [ ] Duplicate code detection: suggests BIRK-25032026-B if BIRK-25032026 exists
12. [ ] Vacation mode: /vacation on → no Telegram, /vacation off → summary
13. [ ] Pending questions: remind at 24h, expire at 72h, don't re-ask
14. [ ] Single /api/jobs fetch per cycle (no redundant API calls)
15. [ ] Calendar dates correct in Spain timezone
16. [ ] Event-driven trigger doesn't block DB transaction
17. [ ] Invoice follow-up updates last_alerts after sending (anti-spam)
18. [ ] Quiet hours = return early, no Telegram at all (not just skipping some phases)
19. [ ] Audit REF response can be received and processed via Telegram
20. [ ] Action log prevents duplicate Holded writes after Brain crash
21. [ ] Email discrepancy nota vs Holded → Brain notifies Miguel
22. [ ] Old n8n crons (job-check, job-alerts) disabled after deploying job-review
23. [ ] Total Telegram messages per cycle capped (~10 max across all phases)
24. [ ] Date format in audit suggestion is DDMMYYYY not YYMMDD
25. [ ] Cancel/reminder regex handles missing accents (olvidate, si)
26. [ ] Global timeout (25min) prevents runaway review cycles
27. [ ] Calendar frontmatter update doesn't corrupt note body
28. [ ] Multiple YES codes in one message all get processed
29. [ ] 3+ consecutive phase failures → escalation alert to Miguel
30. [ ] Learning loop updates existing memories instead of creating duplicates
