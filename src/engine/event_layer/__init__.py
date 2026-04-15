# event_layer — converts articles into stable event objects.
#
# memory.py     — three-way dedup against recently told stories
#                 (duplicate / new_development / new_event).
# clustering.py — cosine-similarity event clustering; groups pool articles
#                 that cover the same real-world event around a representative.
# hotness.py    — event-level hotness model (multi-source log aggregation +
#                 diversity bonus + recency decay).
