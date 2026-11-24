  function harnessEvidenceFactListHtml(facts) {
    return `<dl class="ev-facts">${facts
      .map(fact => {
        const note = fact.note ? `<p class="ev-note">${escapeHtml(fact.note)}</p>` : "";
        return `<dt>${escapeHtml(fact.label)}</dt><dd${fact.mono ? ' class="mono"' : ""}>${escapeHtml(fact.value)}${note}</dd>`;
      })
      .join("")}</dl>`;
  }
