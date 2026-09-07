-- E-commerce schema — MySQL dialect.
-- Exercises: composite PK (junction + line items), 1:1 via UNIQUE FK (payments),
-- unbounded child (order_events), inline FKs, backtick quoting, a view.

CREATE TABLE `customers` (
  `customer_id` INT NOT NULL AUTO_INCREMENT,
  `email` VARCHAR(255) NOT NULL,
  `name` VARCHAR(120) NOT NULL,
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`customer_id`),
  UNIQUE KEY `uq_customers_email` (`email`)
) ENGINE=InnoDB;

CREATE TABLE `addresses` (
  `address_id` INT NOT NULL AUTO_INCREMENT,
  `customer_id` INT NOT NULL,
  `label` VARCHAR(30) NOT NULL,
  `line1` VARCHAR(120) NOT NULL,
  `line2` VARCHAR(120) NULL,
  `city` VARCHAR(80) NOT NULL,
  `country_code` CHAR(2) NOT NULL,
  `postal_code` VARCHAR(16) NOT NULL,
  PRIMARY KEY (`address_id`),
  UNIQUE KEY `uq_addresses_customer_label` (`customer_id`, `label`),
  CONSTRAINT `fk_addresses_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`customer_id`) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE `orders` (
  `order_id` BIGINT NOT NULL AUTO_INCREMENT,
  `customer_id` INT NOT NULL,
  `status` VARCHAR(20) NOT NULL,
  `currency` CHAR(3) NOT NULL,
  `total` DECIMAL(12,2) NOT NULL,
  `placed_at` DATETIME NOT NULL,
  PRIMARY KEY (`order_id`),
  KEY `idx_orders_customer` (`customer_id`),
  CONSTRAINT `fk_orders_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`customer_id`)
) ENGINE=InnoDB;

CREATE TABLE `products` (
  `product_id` INT NOT NULL AUTO_INCREMENT,
  `sku` VARCHAR(40) NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  `description` TEXT,
  `price` DECIMAL(10,2) NOT NULL,
  `stock` INT NOT NULL DEFAULT 0,
  PRIMARY KEY (`product_id`),
  UNIQUE KEY `uq_products_sku` (`sku`)
) ENGINE=InnoDB;

CREATE TABLE `categories` (
  `category_id` INT NOT NULL AUTO_INCREMENT,
  `slug` VARCHAR(60) NOT NULL,
  `name` VARCHAR(80) NOT NULL,
  PRIMARY KEY (`category_id`),
  UNIQUE KEY `uq_categories_slug` (`slug`)
) ENGINE=InnoDB;

CREATE TABLE `product_categories` (
  `product_id` INT NOT NULL,
  `category_id` INT NOT NULL,
  PRIMARY KEY (`product_id`, `category_id`),
  CONSTRAINT `fk_pc_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`product_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_pc_category` FOREIGN KEY (`category_id`) REFERENCES `categories` (`category_id`) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE `order_items` (
  `order_id` BIGINT NOT NULL,
  `line_no` INT NOT NULL,
  `product_id` INT NOT NULL,
  `quantity` INT NOT NULL,
  `unit_price` DECIMAL(10,2) NOT NULL,
  PRIMARY KEY (`order_id`, `line_no`),
  CONSTRAINT `fk_items_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_items_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`product_id`)
) ENGINE=InnoDB;

CREATE TABLE `payments` (
  `payment_id` BIGINT NOT NULL AUTO_INCREMENT,
  `order_id` BIGINT NOT NULL,
  `method` VARCHAR(20) NOT NULL,
  `amount` DECIMAL(12,2) NOT NULL,
  `paid_at` DATETIME NULL,
  PRIMARY KEY (`payment_id`),
  UNIQUE KEY `uq_payments_order` (`order_id`),
  CONSTRAINT `fk_payments_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`)
) ENGINE=InnoDB;

CREATE TABLE `order_events` (
  `event_id` BIGINT NOT NULL AUTO_INCREMENT,
  `order_id` BIGINT NOT NULL,
  `event_type` VARCHAR(30) NOT NULL,
  `payload` JSON,
  `event_time` DATETIME NOT NULL,
  PRIMARY KEY (`event_id`),
  KEY `idx_events_order_time` (`order_id`, `event_time`),
  CONSTRAINT `fk_events_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Unmigratable construct on purpose: proves diagnostics fire.
CREATE VIEW `v_customer_totals` AS
  SELECT `c`.`customer_id`, `c`.`name`, SUM(`o`.`total`) AS `lifetime_value`
  FROM `customers` AS `c` JOIN `orders` AS `o` ON `o`.`customer_id` = `c`.`customer_id`
  GROUP BY `c`.`customer_id`, `c`.`name`;
