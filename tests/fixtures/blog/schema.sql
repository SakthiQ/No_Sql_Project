-- Blog schema — PostgreSQL dialect.
-- Exercises: self-referencing FK (comments.parent_id), pure M:N junction,
-- nullable FK, table-level constraints, forward reference (posts → categories
-- before categories exists), heap table without a PK (page_views), CHECK
-- constraint (unmigratable), quoted reserved-word identifier ("order").

CREATE TABLE authors (
  author_id BIGSERIAL PRIMARY KEY,
  email VARCHAR(255) NOT NULL UNIQUE,
  display_name VARCHAR(120) NOT NULL,
  bio TEXT
);

CREATE TABLE posts (
  post_id BIGSERIAL PRIMARY KEY,
  author_id BIGINT NOT NULL,
  category_id INT REFERENCES categories (category_id) ON DELETE SET NULL,
  title VARCHAR(200) NOT NULL,
  slug VARCHAR(220) NOT NULL UNIQUE,
  body TEXT NOT NULL,
  "order" INT NOT NULL DEFAULT 0,
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_posts_author FOREIGN KEY (author_id) REFERENCES authors (author_id)
);

CREATE TABLE categories (
  category_id SERIAL PRIMARY KEY,
  name VARCHAR(80) NOT NULL UNIQUE
);

CREATE TABLE comments (
  comment_id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL,
  parent_id BIGINT REFERENCES comments (comment_id) ON DELETE CASCADE,
  author_email VARCHAR(255) NOT NULL,
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_comments_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
  CHECK (length(body) > 0)
);

CREATE TABLE tags (
  tag_id SERIAL PRIMARY KEY,
  name VARCHAR(60) NOT NULL UNIQUE
);

CREATE TABLE post_tags (
  post_id BIGINT NOT NULL REFERENCES posts (post_id) ON DELETE CASCADE,
  tag_id INT NOT NULL REFERENCES tags (tag_id) ON DELETE CASCADE,
  PRIMARY KEY (post_id, tag_id)
);

-- Heap table: no primary key, append-only, unbounded.
CREATE TABLE page_views (
  post_id BIGINT NOT NULL REFERENCES posts (post_id),
  viewed_at TIMESTAMPTZ NOT NULL,
  referrer TEXT
);
