# Candidate review plugin

`review_candidate` is a parent-only, read-only review boundary. It validates
the exact base and head commits, constrains the diff to the requested scope,
routes one review request through `auxiliary.review`, and writes a receipt
under the active profile's `reviews/` directory.

If the reviewer route is unavailable, the result is `not_reviewed`; it is never
reported as approval. The plugin does not mutate Git or the candidate files.
