-- Seed data sized so that losing an index really hurts (4M orders, 50K users).
CREATE TABLE orders (
    id          bigserial PRIMARY KEY,
    user_id     integer      NOT NULL,
    sku         text         NOT NULL,
    qty         integer      NOT NULL,
    amount      numeric(10,2) NOT NULL,
    currency    text         NOT NULL DEFAULT 'USD',
    status      text         NOT NULL DEFAULT 'paid',
    created_at  timestamptz  NOT NULL DEFAULT now()
);

INSERT INTO orders (user_id, sku, qty, amount, currency, created_at)
SELECT (random() * 49999)::int + 1,
       'SKU-' || lpad(((random() * 199)::int + 1)::text, 3, '0'),
       1 + (random() * 2)::int,
       round((5 + random() * 295)::numeric, 2),
       (ARRAY['USD','USD','USD','EUR','INR'])[1 + (random() * 4)::int],
       now() - (random() * interval '365 days')
FROM generate_series(1, 4000000);

CREATE INDEX idx_orders_user_id ON orders (user_id, created_at DESC);

CREATE TABLE inventory (
    sku       text PRIMARY KEY,
    stock     integer NOT NULL,
    reserved  integer NOT NULL DEFAULT 0
);

INSERT INTO inventory (sku, stock)
SELECT 'SKU-' || lpad(i::text, 3, '0'), 1000000 FROM generate_series(1, 200) AS i;

ANALYZE;
