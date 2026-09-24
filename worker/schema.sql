-- One table, key to JSON.
--
-- The API was written against a key/value store and every route still
-- speaks that way, so modelling accounts, sessions, projects and chats as
-- separate tables would have meant rewriting all of them to gain nothing:
-- there is not one query in the product that joins across those things.
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
