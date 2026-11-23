# AP-008: invalidate evidence on every overlay close

The evidence modal request fence must also apply when the shared overlay controller closes it, including Escape and `M5Overlays.close`, rather than only when the modal's own close button or backdrop calls its wrapper. Otherwise an in-flight response can repopulate a closed modal. Put invalidation in the existing shared close path for this overlay, including the stale-controller fallback; the wrapper remains responsible only for closing it. Focus restoration, other overlay behavior, session authorization, server routes and data remain unchanged. No network cancellation or state mutation is implied.

Tests cover the real overlay controller through Escape with a delayed request, as well as the existing request-fence unit and browser checks. Rollback reverts this frontend change and regenerates its owned artifacts; no migration is needed.
