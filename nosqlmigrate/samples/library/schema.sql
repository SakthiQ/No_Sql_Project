-- Library schema — PostgreSQL dialect.
-- Exercises: 1:1 via PK=FK (book_details — the D04 trigger), lookup tables
-- ("Genres", branches), ALTER TABLE ADD CONSTRAINT (late FK on loans),
-- composite FK (loans → book_copies), unique index via CREATE UNIQUE INDEX,
-- quoted mixed-case identifiers, junction with bounded side (book_authors).

CREATE TABLE "Genres" (
  genre_id SERIAL PRIMARY KEY,
  genre_name VARCHAR(60) NOT NULL
);

CREATE TABLE branches (
  branch_id SERIAL PRIMARY KEY,
  branch_name VARCHAR(120) NOT NULL,
  city VARCHAR(80) NOT NULL
);

CREATE TABLE authors (
  author_id SERIAL PRIMARY KEY,
  full_name VARCHAR(120) NOT NULL
);

CREATE TABLE books (
  book_id SERIAL PRIMARY KEY,
  title VARCHAR(200) NOT NULL,
  publisher VARCHAR(120),
  publication_year INT,
  genre_id INT NOT NULL REFERENCES "Genres" (genre_id)
);

-- 1:1 detail split: PK is also the FK.
CREATE TABLE book_details (
  book_id INT PRIMARY KEY REFERENCES books (book_id),
  isbn VARCHAR(20) NOT NULL UNIQUE,
  synopsis TEXT,
  page_count INT
);

CREATE TABLE book_authors (
  book_id INT NOT NULL REFERENCES books (book_id) ON DELETE CASCADE,
  author_id INT NOT NULL REFERENCES authors (author_id) ON DELETE CASCADE,
  PRIMARY KEY (book_id, author_id)
);

CREATE TABLE book_copies (
  copy_id SERIAL PRIMARY KEY,
  book_id INT NOT NULL REFERENCES books (book_id),
  copy_no INT NOT NULL,
  branch_id INT NOT NULL REFERENCES branches (branch_id),
  status VARCHAR(12) NOT NULL DEFAULT 'available'
);

-- Uniqueness arrives via a unique index, not a constraint declaration:
-- one of the five paths Table.is_unique() must cover.
CREATE UNIQUE INDEX uq_copies_book_no ON book_copies (book_id, copy_no);

CREATE TABLE members (
  member_id BIGSERIAL PRIMARY KEY,
  full_name VARCHAR(120) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE loans (
  loan_id BIGSERIAL PRIMARY KEY,
  member_id BIGINT NOT NULL,
  book_id INT NOT NULL,
  copy_no INT NOT NULL,
  borrowed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  due_at TIMESTAMPTZ NOT NULL,
  returned_at TIMESTAMPTZ,
  FOREIGN KEY (book_id, copy_no) REFERENCES book_copies (book_id, copy_no)
);

-- FK added after the fact, the way real dumps arrive.
ALTER TABLE loans ADD CONSTRAINT fk_loans_member
  FOREIGN KEY (member_id) REFERENCES members (member_id);
