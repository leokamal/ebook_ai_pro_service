-- The app asks for a document's language on first open and saves it.
-- Row ownership is already enforced by the documents_update policy.
grant update (language) on public.documents to authenticated;
