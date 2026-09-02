"use client";

import { useFieldArray, useForm, useWatch } from "react-hook-form";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { FlowFieldType, Monitor, MonitorType } from "@/lib/types";
type Field = {
  selector: string;
  field_type: FlowFieldType;
  value: string;
  position: number;
};
type Values = {
  monitor_type: MonitorType;
  is_enabled: boolean;
  interval_seconds: number;
  timeout_seconds: number;
  expected_text: string;
  expected_selector: string;
  submit_selector: string;
  success_text: string;
  success_selector: string;
  success_url_contains: string;
  fields: Field[];
  flow_submission_confirmed: boolean;
};
export function MonitorForm({
  websiteId,
  monitor,
}: {
  websiteId: number;
  monitor?: Monitor;
}) {
  const router = useRouter(),
    qc = useQueryClient();
  const form = useForm<Values>({
    defaultValues: {
      monitor_type: monitor?.monitor_type ?? "HTTP",
      is_enabled: monitor?.is_enabled ?? true,
      interval_seconds: monitor?.interval_seconds ?? 300,
      timeout_seconds: monitor?.timeout_seconds ?? 10,
      expected_text: monitor?.expected_text ?? "",
      expected_selector: monitor?.expected_selector ?? "",
      submit_selector: monitor?.flow_config?.submit_selector ?? "",
      success_text: monitor?.flow_config?.success_text ?? "",
      success_selector: monitor?.flow_config?.success_selector ?? "",
      success_url_contains: monitor?.flow_config?.success_url_contains ?? "",
      fields: monitor?.flow_config?.fields ?? [
        { selector: "", field_type: "TEXT", value: "", position: 0 },
      ],
      flow_submission_confirmed: false,
    },
  });
  const fields = useFieldArray({ control: form.control, name: "fields" });
  const type = useWatch({ control: form.control, name: "monitor_type" });
  const flowConfirmed = useWatch({
    control: form.control,
    name: "flow_submission_confirmed",
  });
  const mutation = useMutation({
    mutationFn: (v: Values) => {
      const payload: Record<string, unknown> = {
        website: websiteId,
        monitor_type: v.monitor_type,
        is_enabled: v.is_enabled,
        interval_seconds: Number(v.interval_seconds),
        timeout_seconds: Number(v.timeout_seconds),
        expected_text:
          v.monitor_type === "BROWSER" ? v.expected_text || null : null,
        expected_selector:
          v.monitor_type === "BROWSER" ? v.expected_selector || null : null,
      };
      if (v.monitor_type === "FLOW")
        payload.flow_config = {
          flow_kind: "CONTACT_FORM",
          submit_selector: v.submit_selector,
          success_text: v.success_text || null,
          success_selector: v.success_selector || null,
          success_url_contains: v.success_url_contains || null,
          fields: v.fields.map((f, index) => ({ ...f, position: index })),
        };
      return api<Monitor>(monitor ? `monitors/${monitor.id}` : "monitors", {
        method: monitor ? "PATCH" : "POST",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: async (saved) => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["monitors", String(websiteId)] }),
        qc.invalidateQueries({ queryKey: ["dashboard"] }),
      ]);
      router.push(`/websites/${websiteId}/monitors/${saved.id}`);
    },
  });
  return (
    <form
      className="card stack max-w-3xl p-6"
      onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
    >
      <label className="label">
        Monitor type
        <select className="input" {...form.register("monitor_type")}>
          <option value="HTTP">HTTP availability</option>
          <option value="BROWSER">Rendered browser</option>
          <option value="FLOW">Contact form</option>
        </select>
      </label>
      <div className="rounded-xl border border-[#dce3de] bg-[#f7f9f7] p-4 text-sm">
        {type === "HTTP" &&
          "HTTP checks whether the server responds successfully. It does not render page JavaScript."}
        {type === "BROWSER" &&
          "Browser opens the rendered page and can require specific text or an element selector."}
        {type === "FLOW" &&
          "Contact form opens the page, fills configured fields, and submits the real form."}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="label">
          Check interval (seconds)
          <input
            className="input"
            type="number"
            min="60"
            max="86400"
            {...form.register("interval_seconds", { valueAsNumber: true })}
          />
        </label>
        <label className="label">
          Timeout (seconds)
          <input
            className="input"
            type="number"
            min="1"
            max="30"
            {...form.register("timeout_seconds", { valueAsNumber: true })}
          />
        </label>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" {...form.register("is_enabled")} />
        Enable scheduled checks
      </label>
      {type === "BROWSER" && (
        <div className="stack border-t border-[#edf0ee] pt-5">
          <label className="label">
            Expected text
            <input className="input" {...form.register("expected_text")} />
            <span className="muted text-xs">
              Fail this check if this text is missing.
            </span>
          </label>
          <label className="label">
            Expected selector
            <input
              className="input"
              placeholder="#ready"
              {...form.register("expected_selector")}
            />
            <span className="muted text-xs">
              Fail this check if this page element is missing.
            </span>
          </label>
        </div>
      )}
      {type === "FLOW" && (
        <div className="stack border-t border-[#edf0ee] pt-5">
          <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
            <strong>This check submits a real form.</strong>
            <p className="mt-1">
              Use test-safe values and avoid actions that create real orders,
              charges, bookings, or other irreversible effects.
            </p>
          </div>
          <label className="flex items-start gap-3 rounded-xl border border-amber-300 p-4 text-sm">
            <input
              className="mt-1"
              type="checkbox"
              {...form.register("flow_submission_confirmed", {
                validate: (value) =>
                  type !== "FLOW" ||
                  value ||
                  "Confirm real form submissions to continue.",
              })}
            />
            <span>
              <strong>
                I understand this monitor performs real submissions.
              </strong>
              <span className="muted mt-1 block">
                I have permission to test this form and the supplied values are
                safe.
              </span>
            </span>
          </label>
          <label className="label">
            Submit button selector
            <input
              className="input"
              placeholder="button[type=submit]"
              required
              {...form.register("submit_selector")}
            />
          </label>
          <fieldset className="stack">
            <legend className="font-semibold">
              Success assertions{" "}
              <span className="muted font-normal text-sm">(at least one)</span>
            </legend>
            <label className="label">
              Success text
              <input className="input" {...form.register("success_text")} />
            </label>
            <label className="label">
              Success selector
              <input className="input" {...form.register("success_selector")} />
            </label>
            <label className="label">
              Success URL contains
              <input
                className="input"
                {...form.register("success_url_contains")}
              />
            </label>
          </fieldset>
          <fieldset className="stack">
            <legend className="font-semibold">Fields</legend>
            {fields.fields.map((field, index) => (
              <div
                className="grid gap-3 rounded-xl border border-[#dce3de] p-4 sm:grid-cols-[1.2fr_.7fr_1fr_auto]"
                key={field.id}
              >
                <label className="label">
                  Selector
                  <input
                    className="input"
                    required
                    {...form.register(`fields.${index}.selector`)}
                  />
                </label>
                <label className="label">
                  Type
                  <select
                    className="input"
                    {...form.register(`fields.${index}.field_type`)}
                  >
                    {["TEXT", "EMAIL", "TEXTAREA", "CHECKBOX", "SELECT"].map(
                      (t) => (
                        <option key={t}>{t}</option>
                      ),
                    )}
                  </select>
                </label>
                <label className="label">
                  Test-safe value
                  <input
                    className="input"
                    type="password"
                    autoComplete="off"
                    {...form.register(`fields.${index}.value`)}
                  />
                </label>
                <button
                  type="button"
                  className="button secondary self-end"
                  onClick={() => fields.remove(index)}
                >
                  Remove
                </button>
              </div>
            ))}
            <button
              type="button"
              className="button secondary justify-self-start"
              onClick={() =>
                fields.append({
                  selector: "",
                  field_type: "TEXT",
                  value: "",
                  position: fields.fields.length,
                })
              }
            >
              Add field
            </button>
            <p className="muted text-xs">
              Values are requested only on this configuration screen and are
              never placed in dashboard or activity data.
            </p>
          </fieldset>
        </div>
      )}
      {mutation.error && (
        <p role="alert" className="text-sm text-red-700">
          {mutation.error instanceof ApiError
            ? mutation.error.message
            : "The monitor could not be saved."}
        </p>
      )}
      <div>
        <button
          className="button"
          disabled={mutation.isPending || (type === "FLOW" && !flowConfirmed)}
        >
          {mutation.isPending
            ? "Saving…"
            : monitor
              ? "Save monitor"
              : "Create monitor"}
        </button>
      </div>
    </form>
  );
}
