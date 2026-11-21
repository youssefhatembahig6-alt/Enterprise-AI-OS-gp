import { Alert, Button, Card, StatusPill } from "@eaios/ui";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "./api";
import {
  cancelReport,
  fetchDocumentCitation,
  fetchEvidence,
  resumeReport,
  startReport,
  type AskMode,
  type Citation,
  type DocumentCitation,
  type DonePayload,
  type Evidence,
  type ReportEvent,
  type ReportTable,
} from "./reports";

const EXAMPLE = "What were net (tax-exclusive) fulfilled sales for Q1 2026, in USD?";
const MAX_RECONNECTS = 5;

interface Step {
  step: string;
  label: string;
  state: string;
  attempt?: number;
  maxAttempts?: number;
}

interface Sentence {
  text: string;
  citations: string[];
}

interface RunView {
  runId: string | null;
  lastId: number;
  steps: Step[];
  title: string | null;
  table: ReportTable | null;
  definition: Record<string, unknown> | null;
  sentences: Sentence[];
  citations: Citation[];
  done: DonePayload | null;
  error: { code: string; message: string } | null;
  fakeLabel: string | null;
  reconnecting: boolean;
}

const EMPTY: RunView = {
  runId: null,
  lastId: 0,
  steps: [],
  title: null,
  table: null,
  definition: null,
  sentences: [],
  citations: [],
  done: null,
  error: null,
  fakeLabel: null,
  reconnecting: false,
};

function newRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function apply(view: RunView, event: ReportEvent): RunView {
  const next: RunView = { ...view, reconnecting: false };
  if (event.id !== null) next.lastId = Math.max(view.lastId, event.id);
  const d = event.data;
  if (typeof d.inference_label === "string") next.fakeLabel = d.inference_label;
  if (!next.runId && typeof d.run_id === "string") next.runId = d.run_id;
  switch (event.event) {
    case "status": {
      const step: Step = {
        step: String(d.step),
        label: String(d.label),
        state: String(d.state),
        attempt: typeof d.attempt === "number" ? d.attempt : undefined,
        maxAttempts: typeof d.max_attempts === "number" ? d.max_attempts : undefined,
      };
      const others = view.steps.filter((s) => s.step !== step.step);
      next.steps = [...others, step];
      break;
    }
    case "token":
      if (d.part === "table") {
        next.table = d.table as ReportTable;
        next.title = typeof d.title === "string" ? d.title : null;
        next.definition = (d.definition as Record<string, unknown>) ?? null;
      } else if (d.part === "text") {
        next.sentences = [
          ...view.sentences,
          { text: String(d.text), citations: (d.citations as string[]) ?? [] },
        ];
      }
      break;
    case "citation":
      next.citations = [...view.citations, d as unknown as Citation];
      break;
    case "error":
      next.error = { code: String(d.code), message: String(d.message) };
      break;
    case "done":
      next.done = d as unknown as DonePayload;
      break;
    case "approval_required":
      break; // reserved for wave 3; never sent in M2
  }
  return next;
}

function verificationText(done: DonePayload): { tone: "ok" | "warn"; text: string } | null {
  const v = done.verification;
  if (!v) return null;
  if (v.label === "answer_quotes_verified_dev_checked") {
    return {
      tone: "ok",
      text: "Every sentence quotes an exact passage of a document you can access (re-checked at publication); claims checked by the development baseline checker (not the final trained checker).",
    };
  }
  if (v.narrative === "verified") {
    return {
      tone: "ok",
      text: "Figures verified exactly against the query result; summary checked by the development baseline checker (not the final trained checker).",
    };
  }
  const why: Record<string, string> = {
    withheld_checker_unavailable: "the claim checker was unavailable",
    withheld_failed_verification: "no summary passed verification",
    withheld_model_unavailable: "the model was unavailable for the summary",
  };
  return {
    tone: "warn",
    text: `Figures verified exactly against the query result. No written summary is shown because ${why[v.narrative] ?? "it could not be verified"}.`,
  };
}

function EvidencePanel({
  token,
  citation,
  onClose,
}: {
  token: string;
  citation: Citation;
  onClose: () => void;
}) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setEvidence(null);
    setError(null);
    fetchEvidence(token, citation.evidence_id ?? "")
      .then((e) => live && setEvidence(e))
      .catch((err: unknown) => {
        if (!live) return;
        setError(
          err instanceof ApiError && err.status === 404
            ? "This evidence is not available to you (your access may have changed)."
            : "Could not load the evidence.",
        );
      });
    return () => {
      live = false;
    };
  }, [token, citation.evidence_id]);

  return (
    <Card title="Evidence">
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-muted-fg">
          Exact stored query result for <strong>{citation.label}</strong> (row {(citation.row ?? 0) + 1},
          column <code>{citation.column}</code>). Access is re-checked every time it is opened.
        </p>
        {error ? <Alert>{error}</Alert> : null}
        {!evidence && !error ? <p className="text-muted-fg">Loading…</p> : null}
        {evidence ? (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr>
                    {evidence.columns.map((c) => (
                      <th key={c.name} className="border-b border-border px-2 py-1 font-medium">
                        {c.name} <span className="text-muted-fg">({c.type})</span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {evidence.rows.map((row, r) => (
                    <tr key={r}>
                      {row.map((cell, c) => {
                        const hit = r === citation.row && evidence.columns[c]?.name === citation.column;
                        return (
                          <td
                            key={c}
                            className={`px-2 py-1 font-mono ${hit ? "bg-warn-bg text-warn-fg" : ""}`}
                          >
                            {cell === null ? "NULL" : String(cell)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <div className="mb-1 font-medium">Read-only SQL that produced it</div>
              <pre className="overflow-x-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
                {evidence.executed_sql}
              </pre>
              <div className="mt-1 text-xs text-muted-fg">
                Bound parameters: <code>{JSON.stringify(evidence.parameters)}</code>
              </div>
            </div>
            <dl className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-3">
              <dt className="text-muted-fg">Integrity</dt>
              <dd className="sm:col-span-2">
                <StatusPill tone={evidence.integrity === "verified" ? "ok" : "error"}>
                  {evidence.integrity === "verified" ? "hash verified" : "hash mismatch"}
                </StatusPill>
              </dd>
              <dt className="text-muted-fg">Result SHA-256</dt>
              <dd className="font-mono break-all sm:col-span-2">{evidence.result_sha256}</dd>
              <dt className="text-muted-fg">Dataset</dt>
              <dd className="font-mono break-all sm:col-span-2">{evidence.dataset_fingerprint}</dd>
              <dt className="text-muted-fg">Executed</dt>
              <dd className="sm:col-span-2">{new Date(evidence.executed_at).toLocaleString()}</dd>
              <dt className="text-muted-fg">Policy version</dt>
              <dd className="sm:col-span-2">{evidence.policy_epoch}</dd>
            </dl>
          </>
        ) : null}
        <div className="flex justify-end">
          <Button variant="secondary" onClick={onClose}>
            Close evidence
          </Button>
        </div>
      </div>
    </Card>
  );
}

function DocumentCitationPanel({
  token,
  runId,
  citation,
  onClose,
}: {
  token: string;
  runId: string;
  citation: Citation;
  onClose: () => void;
}) {
  const [passage, setPassage] = useState<DocumentCitation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setPassage(null);
    setError(null);
    fetchDocumentCitation(token, runId, citation.citation_id)
      .then((p) => live && setPassage(p))
      .catch((err: unknown) => {
        if (!live) return;
        setError(
          err instanceof ApiError && err.status === 404
            ? "This source is not available to you (your access may have changed or the document was removed)."
            : "Could not load the source.",
        );
      });
    return () => {
      live = false;
    };
  }, [token, runId, citation.citation_id]);

  return (
    <Card title="Source">
      <div className="flex flex-col gap-3 text-sm">
        {error ? <Alert>{error}</Alert> : null}
        {!passage && !error ? <p className="text-muted-fg">Loading…</p> : null}
        {passage ? (
          <>
            <p className="text-muted-fg">
              <strong>{passage.title}</strong> · {passage.filename} · version {passage.version}
              {passage.is_current_version ? "" : ` (current is ${passage.current_version}; this is the version the answer used)`}
            </p>
            <blockquote className="rounded-md bg-muted p-3 whitespace-pre-wrap break-words">
              <span className="text-muted-fg">…{passage.before}</span>
              <mark className="bg-warn-bg text-warn-fg">{passage.quote}</mark>
              <span className="text-muted-fg">{passage.after}…</span>
            </blockquote>
            <p className="text-xs text-muted-fg">
              Characters {passage.char_start}–{passage.char_end} (Unicode code points) ·{" "}
              <StatusPill tone={passage.integrity === "verified" ? "ok" : "error"}>
                {passage.integrity === "verified" ? "text hash verified" : "text hash mismatch"}
              </StatusPill>
            </p>
          </>
        ) : null}
        <div className="flex justify-end">
          <Button variant="secondary" onClick={onClose}>
            Close source
          </Button>
        </div>
      </div>
    </Card>
  );
}

export function ReportPanel({ token, packs }: { token: string; packs: string[] | null }) {
  // Menus follow the tenant's packs; the backend refuses disabled packs regardless.
  const docsOn = packs === null || packs.includes("documents");
  const reportsOn = packs === null || packs.includes("sales_reports");
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<AskMode>("auto");
  const [view, setView] = useState<RunView>(EMPTY);
  const [running, setRunning] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [selected, setSelected] = useState<Citation | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const viewRef = useRef<RunView>(EMPTY);

  const onEvent = useCallback((event: ReportEvent) => {
    viewRef.current = apply(viewRef.current, event);
    setView(viewRef.current);
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  const run = async (event: FormEvent) => {
    event.preventDefault();
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    viewRef.current = EMPTY;
    setView(EMPTY);
    setSelected(null);
    setFailure(null);
    setRunning(true);
    try {
      const { runId, ended } = await startReport(
        token,
        question.trim(),
        newRequestId(),
        onEvent,
        controller.signal,
        mode,
      );
      if (runId && !viewRef.current.runId) {
        viewRef.current = { ...viewRef.current, runId };
        setView(viewRef.current);
      }
      let state = ended;
      for (let attempt = 1; state === "dropped" && attempt <= MAX_RECONNECTS; attempt++) {
        const id = viewRef.current.runId;
        if (!id || controller.signal.aborted) break;
        viewRef.current = { ...viewRef.current, reconnecting: true };
        setView(viewRef.current);
        await new Promise((r) => setTimeout(r, Math.min(1000 * attempt, 4000)));
        try {
          state = await resumeReport(token, id, viewRef.current.lastId, onEvent, controller.signal);
        } catch (err) {
          if (err instanceof ApiError) throw err;
          state = "dropped";
        }
      }
      if (state === "dropped" && !controller.signal.aborted) {
        setFailure("Lost the connection to the server. The run may still finish; try again later.");
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof ApiError) {
        setFailure(
          err.status === 503
            ? "Reporting is not configured on this server."
            : err.status === 401
              ? "Your session has ended. Sign in again."
              : err.message,
        );
      } else {
        setFailure("Could not reach the API.");
      }
    } finally {
      setRunning(false);
    }
  };

  const cancel = async () => {
    const id = viewRef.current.runId;
    if (!id) return;
    try {
      await cancelReport(token, id);
    } catch {
      setFailure("Could not cancel the request.");
    }
  };

  const cite = (id: string) => view.citations.find((c) => c.citation_id === id) ?? null;
  const citationNumber = (id: string) => view.citations.findIndex((c) => c.citation_id === id) + 1;
  const verification = view.done ? verificationText(view.done) : null;
  const fake = view.fakeLabel;

  return (
    <div className="flex flex-col gap-6">
      <Card title="Ask">
        <form className="flex flex-col gap-3" onSubmit={(e) => void run(e)}>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label htmlFor="ask-mode" className="font-medium">
              Source
            </label>
            <select
              id="ask-mode"
              className="h-9 rounded-md border border-border bg-bg px-2"
              value={mode}
              onChange={(e) => setMode(e.target.value as AskMode)}
            >
              <option value="auto">Automatic</option>
              {docsOn ? <option value="documents">Documents you can access</option> : null}
              {reportsOn ? <option value="report">Sales report (SQL)</option> : null}
            </select>
          </div>
          <label htmlFor="question" className="text-sm font-medium">
            Ask about your documents or fulfilled sales
          </label>
          <textarea
            id="question"
            rows={2}
            maxLength={1000}
            className="rounded-md border border-border bg-bg px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-ring"
            placeholder={EXAMPLE}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={running || !question.trim()}>
              {running ? "Working…" : "Run report"}
            </Button>
            {running ? (
              <Button type="button" variant="secondary" onClick={() => void cancel()}>
                Cancel
              </Button>
            ) : (
              <Button type="button" variant="secondary" onClick={() => setQuestion(EXAMPLE)}>
                Use example
              </Button>
            )}
          </div>
        </form>
        {fake ? (
          <div className="mt-4">
            <StatusPill tone="warn">{fake}</StatusPill>
          </div>
        ) : null}
        {view.steps.length > 0 ? (
          <ol className="mt-4 flex flex-col gap-1 text-sm" aria-live="polite">
            {view.steps.map((s) => (
              <li key={s.step} className="flex items-center gap-2">
                <span aria-hidden>{s.state === "retrying" ? "↻" : "✓"}</span>
                <span>
                  {s.label}
                  {s.state === "retrying" && s.attempt
                    ? ` (repair ${s.attempt - 1} of ${(s.maxAttempts ?? 3) - 1})`
                    : ""}
                </span>
              </li>
            ))}
            {view.reconnecting ? <li className="text-muted-fg">Reconnecting…</li> : null}
          </ol>
        ) : null}
        {failure ? (
          <div className="mt-4">
            <Alert>{failure}</Alert>
          </div>
        ) : null}
        {view.error ? (
          <div className="mt-4">
            <Alert>{view.error.message}</Alert>
          </div>
        ) : null}
        {view.done?.abstention ? (
          <p className="mt-4 rounded-md bg-muted px-3 py-2 text-sm">{view.done.abstention.message}</p>
        ) : null}
      </Card>

      {view.table && view.done?.outcome === "published" ? (
        <Card title={view.title ?? "Verified result"}>
          {verification ? (
            <div className="mb-4 flex flex-col gap-1">
              <StatusPill tone={verification.tone}>
                {verification.tone === "ok" ? "Verified" : "Figures verified, summary withheld"}
              </StatusPill>
              <p className="text-xs text-muted-fg">{verification.text}</p>
            </div>
          ) : null}
          <dl className="divide-y divide-border">
            {view.table.rows[0]?.map((cell) => {
              const column = view.table?.columns.find((c) => c.key === cell.column);
              const citation = cite(cell.citation_id);
              return (
                <div key={cell.column} className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-3">
                  <dt className="text-sm text-muted-fg">{column?.label ?? cell.column}</dt>
                  <dd className="text-sm font-medium tabular-nums sm:col-span-2">
                    {cell.display}{" "}
                    {citation ? (
                      <button
                        type="button"
                        className="ml-1 rounded px-1 text-xs text-primary underline"
                        onClick={() => setSelected(citation)}
                        aria-label={`Show evidence for ${column?.label ?? cell.column}`}
                      >
                        [{citationNumber(citation.citation_id)}]
                      </button>
                    ) : null}
                  </dd>
                </div>
              );
            })}
          </dl>
          {view.sentences.length > 0 ? (
            <div className="mt-4 flex flex-col gap-2 text-sm">
              {view.sentences.map((s, i) => (
                <p key={i}>
                  {s.text}{" "}
                  {s.citations.map((id) => {
                    const c = cite(id);
                    return c ? (
                      <button
                        key={id}
                        type="button"
                        className="rounded px-0.5 text-xs text-primary underline"
                        onClick={() => setSelected(c)}
                      >
                        [{citationNumber(id)}]
                      </button>
                    ) : null;
                  })}
                </p>
              ))}
            </div>
          ) : null}
          {view.definition ? (
            <p className="mt-4 text-xs text-muted-fg">
              Definition: order date basis; included statuses{" "}
              {(view.definition.included_statuses as string[]).join(", ")} (excluded{" "}
              {(view.definition.excluded_statuses as string[]).join(", ") || "none"});{" "}
              {String(view.definition.tax_treatment)}; currency {String(view.definition.currency)}.
            </p>
          ) : null}
        </Card>
      ) : null}

      {!view.table && view.done?.outcome === "published" && view.sentences.length > 0 ? (
        <Card title="Answer">
          {verification ? (
            <div className="mb-4 flex flex-col gap-1">
              <StatusPill tone={verification.tone}>Verified against sources</StatusPill>
              <p className="text-xs text-muted-fg">{verification.text}</p>
            </div>
          ) : null}
          <div className="flex flex-col gap-2 text-sm">
            {view.sentences.map((s, i) => (
              <p key={i}>
                {s.text}{" "}
                {s.citations.map((id) => {
                  const c = cite(id);
                  return c ? (
                    <button
                      key={id}
                      type="button"
                      className="rounded px-0.5 text-xs text-primary underline"
                      onClick={() => setSelected(c)}
                      aria-label={`Open source: ${c.title ?? "document"}`}
                    >
                      [{citationNumber(id)}]
                    </button>
                  ) : null;
                })}
              </p>
            ))}
          </div>
          <ul className="mt-4 flex flex-col gap-1 text-xs text-muted-fg">
            {view.citations.map((c) => (
              <li key={c.citation_id}>
                [{citationNumber(c.citation_id)}] {c.title} (version {c.version}
                {c.page ? `, page ${c.page}` : ""}
                {c.heading_path?.length ? `, ${c.heading_path.join(" › ")}` : ""})
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {selected && selected.document_id && view.runId ? (
        <DocumentCitationPanel
          token={token}
          runId={view.runId}
          citation={selected}
          onClose={() => setSelected(null)}
        />
      ) : selected ? (
        <EvidencePanel token={token} citation={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}
