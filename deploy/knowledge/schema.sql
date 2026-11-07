CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS knowledge_revisions (
  scope uuid NOT NULL, revision uuid NOT NULL, digest text NOT NULL,
  deleted boolean NOT NULL, source_bytes bigint NOT NULL, index_bytes bigint NOT NULL,
  chunk_count integer NOT NULL, PRIMARY KEY(scope, revision)
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  scope uuid NOT NULL, revision uuid NOT NULL, ordinal integer NOT NULL,
  filename text NOT NULL, line integer NOT NULL, content text NOT NULL, embedding vector(512) NOT NULL,
  PRIMARY KEY(scope, revision, ordinal),
  FOREIGN KEY(scope, revision) REFERENCES knowledge_revisions(scope, revision)
);
