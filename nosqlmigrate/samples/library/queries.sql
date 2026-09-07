-- Library workload. Same format as the other fixtures.

-- @weight 45 read  book page: book + detail together (the D04 case)
SELECT b.book_id, b.title, b.publisher, b.publication_year,
       d.isbn, d.synopsis, d.page_count
FROM books b
JOIN book_details d ON d.book_id = b.book_id
WHERE b.book_id = ?;

-- @weight 8 read  book with its genre name
SELECT b.book_id, b.title, g.genre_name
FROM books b
JOIN "Genres" g ON g.genre_id = b.genre_id
WHERE b.book_id = ?;

-- @weight 5 read  catalog search (books standalone)
SELECT book_id, title, publication_year
FROM books
WHERE title ILIKE ?
ORDER BY publication_year DESC
LIMIT 25;

-- @weight 12 read  copies of a book with branch names
SELECT c.copy_id, c.copy_no, c.status, br.branch_name, br.city
FROM book_copies c
JOIN branches br ON br.branch_id = c.branch_id
WHERE c.book_id = ?;

-- @weight 15 read  a member's active loans with book titles
SELECT l.loan_id, l.borrowed_at, l.due_at, b.title
FROM loans l
JOIN books b ON b.book_id = l.book_id
WHERE l.member_id = ? AND l.returned_at IS NULL;

-- @weight 6 read  overdue report (loans standalone)
SELECT loan_id, due_at
FROM loans
WHERE due_at < ? AND returned_at IS NULL;

-- @weight 10 read  member profile (members standalone)
SELECT member_id, full_name, email, joined_at
FROM members
WHERE member_id = ?;

-- @weight 5 read  books by author
SELECT b.book_id, b.title
FROM books b
JOIN book_authors ba ON ba.book_id = b.book_id
JOIN authors a ON a.author_id = ba.author_id
WHERE a.author_id = ?;

-- @weight 4 read  author page (authors standalone)
SELECT author_id, full_name
FROM authors
WHERE author_id = ?;

-- @weight 20 write checkout: create a loan
INSERT INTO loans (member_id, book_id, copy_no, borrowed_at, due_at) VALUES (?, ?, ?, ?, ?);

-- @weight 20 write checkout: mark the copy checked out
UPDATE book_copies SET status = 'checked_out' WHERE book_id = ? AND copy_no = ?;

-- @weight 18 write return: close the loan
UPDATE loans SET returned_at = ? WHERE loan_id = ?;

-- @weight 18 write return: mark the copy available
UPDATE book_copies SET status = 'available' WHERE book_id = ? AND copy_no = ?;

-- @weight 3 write catalog a new book
INSERT INTO books (title, publisher, publication_year, genre_id) VALUES (?, ?, ?, ?);

-- @weight 3 write add the book's detail record
INSERT INTO book_details (book_id, isbn, synopsis, page_count) VALUES (?, ?, ?, ?);

-- @weight 3 write assign an author to a book
INSERT INTO book_authors (book_id, author_id) VALUES (?, ?);
