"use client";

import { type FormEvent, useState } from "react";
import type { Condition, JobKind, JobSpec, Schedule } from "@/client/types.gen";
import { Button } from "@/components/ui/button";

const TIMEZONE = "America/Toronto";
const input =
  "w-full rounded-md border bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring/40";

type ScheduleKind = Schedule["kind"];
type ConditionKind = Condition["kind"];

/** `datetime-local` value (local wall time) <-> ISO with offset. */
function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is passed as a child
    <label className="block space-y-1">
      <span className="text-muted-foreground text-xs">{label}</span>
      {children}
    </label>
  );
}

/** Create or edit a job. Field state is flat; the spec is assembled on submit. */
export function JobForm({
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial?: JobSpec;
  submitLabel: string;
  onSubmit: (spec: JobSpec) => Promise<string | null>;
  onCancel?: () => void;
}) {
  const s = initial?.schedule;
  const c = initial?.watch?.condition;
  const [name, setName] = useState(initial?.name ?? "");
  const [kind, setKind] = useState<JobKind>(initial?.kind ?? "watch");
  const [scheduleKind, setScheduleKind] = useState<ScheduleKind>(
    s?.kind ?? "interval",
  );
  const [expression, setExpression] = useState(
    s?.kind === "cron" ? s.expression : "0 8 * * *",
  );
  const [everyHours, setEveryHours] = useState(
    s?.kind === "interval" ? String(s.every_seconds / 3600) : "6",
  );
  const [at, setAt] = useState(
    s?.kind === "one_shot" ? toLocalInput(s.at) : "",
  );
  const [prompt, setPrompt] = useState(initial?.prompt ?? "");
  const [target, setTarget] = useState(initial?.watch?.target ?? "");
  const [extract, setExtract] = useState(initial?.watch?.extract ?? "");
  const [conditionKind, setConditionKind] = useState<ConditionKind>(
    c?.kind ?? "below",
  );
  const [threshold, setThreshold] = useState(
    c && (c.kind === "below" || c.kind === "above") ? String(c.threshold) : "",
  );
  const [text, setText] = useState(c?.kind === "contains" ? c.text : "");
  const [cooldownHours, setCooldownHours] = useState(
    String((initial?.watch?.cooldown_seconds ?? 21600) / 3600),
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function schedule(): Schedule {
    if (scheduleKind === "cron")
      return { kind: "cron", expression, timezone: TIMEZONE };
    if (scheduleKind === "interval") {
      return {
        kind: "interval",
        every_seconds: Math.round(Number(everyHours) * 3600),
      };
    }
    return { kind: "one_shot", at: new Date(at).toISOString() };
  }

  function condition(): Condition {
    if (conditionKind === "contains") return { kind: "contains", text };
    if (conditionKind === "changed")
      return { kind: "changed", min_delta: Number(threshold) || 0 };
    return { kind: conditionKind, threshold: Number(threshold) };
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    const spec: JobSpec = {
      name,
      kind,
      schedule: schedule(),
      prompt: kind === "task" ? prompt : null,
      watch:
        kind === "watch"
          ? {
              target,
              extract,
              condition: condition(),
              cooldown_seconds: Math.round(Number(cooldownHours) * 3600),
            }
          : null,
      expires_at: initial?.expires_at ?? null,
      model: initial?.model ?? null,
    };
    const failure = await onSubmit(spec);
    setSaving(false);
    if (failure) setError(failure);
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg border p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name">
          <input
            className={input}
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field label="Kind">
          <select
            className={input}
            value={kind}
            onChange={(e) => setKind(e.target.value as JobKind)}
          >
            <option value="watch">
              Watch: check a value and alert on a condition
            </option>
            <option value="task">Task: run a prompt</option>
          </select>
        </Field>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Schedule">
          <select
            className={input}
            value={scheduleKind}
            onChange={(e) => setScheduleKind(e.target.value as ScheduleKind)}
          >
            <option value="interval">Every N hours</option>
            <option value="cron">Cron (local time, {TIMEZONE})</option>
            <option value="one_shot">Once</option>
          </select>
        </Field>
        {scheduleKind === "cron" && (
          <Field label="Cron expression (minute hour day month weekday)">
            <input
              className={`${input} font-mono`}
              value={expression}
              onChange={(e) => setExpression(e.target.value)}
            />
          </Field>
        )}
        {scheduleKind === "interval" && (
          <Field label="Every (hours, at least 0.1)">
            <input
              className={input}
              type="number"
              min="0.1"
              step="0.1"
              value={everyHours}
              onChange={(e) => setEveryHours(e.target.value)}
            />
          </Field>
        )}
        {scheduleKind === "one_shot" && (
          <Field label="At (your local time)">
            <input
              className={input}
              type="datetime-local"
              value={at}
              onChange={(e) => setAt(e.target.value)}
              required
            />
          </Field>
        )}
      </div>

      {kind === "task" ? (
        <Field label="What should Kairos do each time?">
          <textarea
            className={input}
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            required
          />
        </Field>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Target: a URL or a search query">
              <input
                className={input}
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                required
              />
            </Field>
            <Field label="Extract: what to read from it">
              <input
                className={input}
                value={extract}
                onChange={(e) => setExtract(e.target.value)}
                placeholder="lowest round-trip fare in CAD"
                required
              />
            </Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Alert when the value">
              <select
                className={input}
                value={conditionKind}
                onChange={(e) =>
                  setConditionKind(e.target.value as ConditionKind)
                }
              >
                <option value="below">is below</option>
                <option value="above">is above</option>
                <option value="contains">text contains</option>
                <option value="changed">changes by at least</option>
              </select>
            </Field>
            {conditionKind === "contains" ? (
              <Field label="Text">
                <input
                  className={input}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  required
                />
              </Field>
            ) : (
              <Field
                label={
                  conditionKind === "changed" ? "Minimum change" : "Threshold"
                }
              >
                <input
                  className={input}
                  type="number"
                  step="any"
                  value={threshold}
                  onChange={(e) => setThreshold(e.target.value)}
                  required={conditionKind !== "changed"}
                />
              </Field>
            )}
            <Field label="Cooldown between alerts (hours)">
              <input
                className={input}
                type="number"
                min="0"
                step="0.5"
                value={cooldownHours}
                onChange={(e) => setCooldownHours(e.target.value)}
              />
            </Field>
          </div>
        </>
      )}

      {error && <p className="text-destructive text-sm">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" disabled={saving}>
          {saving ? "Saving…" : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}
