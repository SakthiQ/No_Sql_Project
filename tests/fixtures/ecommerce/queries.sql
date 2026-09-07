-- E-commerce workload.
-- Format: a comment line "-- @weight N @tag read|write" before each statement.
-- Weights are relative frequencies. They drive co-access, write-ratio and
-- standalone-access signals; they are assumptions, not measurements.

-- @weight 60 read  order page: order with its line items (the hot path)
SELECT o.order_id, o.status, o.currency, o.total, o.placed_at,
       i.line_no, i.product_id, i.quantity, i.unit_price
FROM orders o
JOIN order_items i ON i.order_id = o.order_id
WHERE o.order_id = ?;

-- @weight 15 read  order with customer info
SELECT o.order_id, o.status, o.total, c.customer_id, c.name, c.email
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id = ?;

-- @weight 22 read  customer profile with saved addresses
SELECT c.customer_id, c.email, c.name, a.label, a.city, a.postal_code
FROM customers c
JOIN addresses a ON a.customer_id = c.customer_id
WHERE c.customer_id = ?;

-- @weight 8 read  customer order history (paginated)
SELECT o.order_id, o.status, o.total, o.placed_at
FROM orders o
WHERE o.customer_id = ?
ORDER BY o.placed_at DESC
LIMIT 20;

-- @weight 6 read  admin console: recent orders across all customers
SELECT o.order_id, o.status, o.total, o.placed_at
FROM orders o
ORDER BY o.placed_at DESC
LIMIT 50;

-- @weight 9 read  product page with its categories
SELECT p.product_id, p.sku, p.title, p.price, pc.category_id
FROM products p
LEFT JOIN product_categories pc ON pc.product_id = p.product_id
WHERE p.product_id = ?;

-- @weight 7 read  products in a category
SELECT p.product_id, p.title, p.price
FROM products p
JOIN product_categories pc ON pc.product_id = p.product_id
WHERE pc.category_id = ?;

-- @weight 5 read  category browser
SELECT category_id, slug, name
FROM categories
ORDER BY name;

-- @weight 6 read  product detail (standalone)
SELECT product_id, sku, title, description, price, stock
FROM products
WHERE product_id = ?;

-- @weight 3 read  payment status for an order
SELECT o.order_id, o.total, p.method, p.amount, p.paid_at
FROM orders o
JOIN payments p ON p.order_id = o.order_id
WHERE o.order_id = ?;

-- @weight 2 read  recent events for one order
SELECT e.event_type, e.payload, e.event_time
FROM order_events e
WHERE e.order_id = ?
ORDER BY e.event_time DESC
LIMIT 100;

-- @weight 1 read  event analytics (order_events standalone)
SELECT event_type, COUNT(*) AS n
FROM order_events
WHERE event_time >= ?
GROUP BY event_type;

-- @weight 30 write place an order
INSERT INTO orders (customer_id, status, currency, total, placed_at) VALUES (?, 'pending', ?, ?, ?);

-- @weight 30 write add a line item to an order
INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price) VALUES (?, ?, ?, ?, ?);

-- @weight 25 write record an order event
INSERT INTO order_events (order_id, event_type, payload, event_time) VALUES (?, ?, ?, ?);

-- @weight 5 write order status change
UPDATE orders SET status = ? WHERE order_id = ?;

-- @weight 4 write payment recorded for an order
INSERT INTO payments (order_id, method, amount, paid_at) VALUES (?, ?, ?, ?);

-- @weight 2 write stock adjustment
UPDATE products SET stock = stock - ? WHERE product_id = ?;
