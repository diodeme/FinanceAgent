/**
 * Feature flags for services the backend may not expose.
 *
 * `KNOWLEDGE_BASE_ENABLED` mirrors `create_app()` being called without a
 * `knowledge_base_manager` (see backend/finagent/app.py): every
 * `/knowledge_bases` endpoint answers 503, so the UI must not call them
 * unprompted — the chat viewport fetches the list on mount. Flip this
 * together with the backend wiring; steps in docs/数据结构.md §8.
 */
export const KNOWLEDGE_BASE_ENABLED = false;
