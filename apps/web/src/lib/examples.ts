/**
 * Example prompts, each showing one capability. They are word for word the scenes
 * of the scripted demo (tests/e2e/demo.py, checked by tests/e2e/test_demo.py), so
 * they also work under `just api-demo` with no API key.
 */
export type Example = { prompt: string; shows: string };

export const EXAMPLES: readonly Example[] = [
  {
    prompt: "Plan a three-step comparison of Postgres queue libraries.",
    shows: "A plan with a live checklist",
  },
  {
    prompt:
      "Every weekday at 8am, check flights from Toronto to Lisbon in March and tell me if a round trip drops under $700.",
    shows: "A fare watch you confirm",
  },
  {
    prompt:
      "Remember that I prefer aisle seats and usually fly out of Toronto Pearson.",
    shows: "A memory you confirm",
  },
  {
    prompt: "Email me a short packing list for a week in Lisbon in March.",
    shows: "An action that waits for your approval",
  },
];
