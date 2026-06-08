"""Small adaptive selector helpers inspired by resilient scraper frameworks.

This does not include stealth, fingerprint changes, proxying, or anti-bot
workarounds. It only ranks ordinary CSS selectors by visible evidence so the
parser can survive harmless DOM class changes a little better.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SelectorCandidate:
    selector: str
    label: str
    must_contain_any: tuple[str, ...] = ()
    nice_to_have_any: tuple[str, ...] = ()
    max_nodes: int = 40
    sample_nodes: int = 5


def score_selector_observation(
    *,
    count: int,
    text_preview: str,
    must_contain_any: tuple[str, ...] = (),
    nice_to_have_any: tuple[str, ...] = (),
) -> int:
    if count <= 0:
        return -100
    text = str(text_preview or "")
    score = min(count, 20) * 2
    if must_contain_any:
        if any(token and token in text for token in must_contain_any):
            score += 80
        else:
            score -= 80
    score += sum(10 for token in nice_to_have_any if token and token in text)
    if len(text) < 10:
        score -= 10
    return score


async def observe_selector_candidates(page: Any, candidates: list[SelectorCandidate]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            locator = page.locator(candidate.selector)
            count = min(await locator.count(), candidate.max_nodes)
            previews: list[str] = []
            if count:
                for index in range(min(count, max(1, candidate.sample_nodes))):
                    try:
                        preview = (await locator.nth(index).inner_text(timeout=1500)).strip()
                    except Exception:
                        preview = ""
                    if preview:
                        previews.append(preview)
            preview_text = "\n".join(previews)
            score = score_selector_observation(
                count=count,
                text_preview=preview_text,
                must_contain_any=candidate.must_contain_any,
                nice_to_have_any=candidate.nice_to_have_any,
            )
            observations.append(
                {
                    "selector": candidate.selector,
                    "label": candidate.label,
                    "count": count,
                    "sampled_nodes": min(count, max(1, candidate.sample_nodes)),
                    "text_preview": preview_text[:240],
                    "score": score,
                }
            )
        except Exception as exc:
            observations.append(
                {
                    "selector": candidate.selector,
                    "label": candidate.label,
                    "count": 0,
                    "text_preview": "",
                    "score": -100,
                    "error": str(exc),
                }
            )
    return sorted(observations, key=lambda item: int(item.get("score") or -100), reverse=True)
