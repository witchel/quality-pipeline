You are evaluating a complete pipeline run of an automated code quality tool. The pipeline ran multiple rounds, each with a focused objective. Your job is to assess the overall run quality and provide actionable feedback for prompt tuning.

## Pipeline Run Data

CONTEXT_PLACEHOLDER

## Evaluation Criteria

Evaluate the pipeline run against these criteria:

1. **Low-value rounds** — Rounds that produced trivial changes (renaming variables, adding docstrings, reformatting) disproportionate to their cost in time and budget.

2. **Redundant work** — Multiple rounds touching the same files or logic, suggesting overlap in round definitions or scope creep.

3. **Scope violations** — Rounds that went beyond their stated objective (e.g., a "security" round that also refactored unrelated code).

4. **Time efficiency** — Rounds that took disproportionate time relative to their value. A round using 40% of pipeline time for minor cosmetic changes is a problem.

5. **Architecture and maintainability drift** — Rounds that introduced or failed
   to address duplicate implementations, leaky interfaces, broad public surface
   area, unclear module ownership, or brittle tests coupled to internals.

6. **Recommendations** — Actionable suggestions for improving round prompts, reordering rounds, merging redundant rounds, or adjusting budgets.

## Output Format

Respond with a JSON object (and nothing else) in this exact format:

```json
{
  "low_value_rounds": ["round-name: brief explanation", ...],
  "redundant_work": ["description of overlap", ...],
  "scope_violations": ["round-name: what it did outside scope", ...],
  "time_concerns": ["round-name: why time was disproportionate", ...],
  "recommendations": ["actionable suggestion", ...],
  "overall_assessment": "One-sentence summary of pipeline run quality"
}
```

Use empty arrays for categories with no findings. Be specific and actionable — vague observations are not useful.
