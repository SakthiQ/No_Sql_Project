-- Blog workload. Same format as the e-commerce fixture.

-- @weight 35 read  post page with its comments
SELECT p.post_id, p.title, p.body, p.published_at,
       c.comment_id, c.parent_id, c.author_email, c.body AS comment_body, c.created_at
FROM posts p
JOIN comments c ON c.post_id = p.post_id
WHERE p.post_id = ?;

-- @weight 15 read  post with its tags
SELECT p.post_id, p.title, t.name AS tag
FROM posts p
JOIN post_tags pt ON pt.post_id = p.post_id
JOIN tags t ON t.tag_id = pt.tag_id
WHERE p.post_id = ?;

-- @weight 20 read  front page listing (posts standalone)
SELECT post_id, title, slug, published_at
FROM posts
WHERE published_at IS NOT NULL
ORDER BY published_at DESC
LIMIT 20;

-- @weight 10 read  posts by author
SELECT p.post_id, p.title, p.published_at
FROM posts p
WHERE p.author_id = ?
ORDER BY p.published_at DESC;

-- @weight 5 read  author profile (authors standalone)
SELECT author_id, display_name, bio
FROM authors
WHERE author_id = ?;

-- @weight 3 read  flat comment thread for a post (comments without the post row)
SELECT comment_id, parent_id, author_email, body, created_at
FROM comments
WHERE post_id = ?
ORDER BY created_at;

-- @weight 8 read  posts by tag
SELECT p.post_id, p.title
FROM posts p
JOIN post_tags pt ON pt.post_id = p.post_id
JOIN tags t ON t.tag_id = pt.tag_id
WHERE pt.tag_id = ?;

-- @weight 5 read  tag cloud
SELECT t.name, COUNT(*) AS uses
FROM tags t
JOIN post_tags pt ON pt.tag_id = t.tag_id
GROUP BY t.name;

-- @weight 12 read  view counter for a post
SELECT post_id, COUNT(*) AS views
FROM page_views
WHERE post_id = ? AND viewed_at >= ?
GROUP BY post_id;

-- @weight 30 write new comment
INSERT INTO comments (post_id, parent_id, author_email, body) VALUES (?, ?, ?, ?);

-- @weight 20 write publish a post
INSERT INTO posts (author_id, category_id, title, slug, body, "order", published_at)
VALUES (?, ?, ?, ?, ?, ?, ?);

-- @weight 10 write record a page view
INSERT INTO page_views (post_id, viewed_at, referrer) VALUES (?, ?, ?);

-- @weight 8 write tag a post
INSERT INTO post_tags (post_id, tag_id) VALUES (?, ?);

-- @weight 2 write edit a post
UPDATE posts SET body = ?, "order" = ? WHERE post_id = ?;
