-- Fill in your 4 real territory names + realestate.com.au URLs, then run in
-- Supabase → SQL Editor. RETURNING gives you back the new IDs immediately —
-- you'll need those for the hydrate_new_territory.py --lga-id step.

INSERT INTO lgas (name, active, search_url_listings, search_url_sold) VALUES
  ('Territory 1 name', true, 'https://www.realestate.com.au/buy/...', 'https://www.realestate.com.au/sold/...'),
  ('Territory 2 name', true, 'https://www.realestate.com.au/buy/...', 'https://www.realestate.com.au/sold/...'),
  ('Territory 3 name', true, 'https://www.realestate.com.au/buy/...', 'https://www.realestate.com.au/sold/...'),
  ('Territory 4 name', true, 'https://www.realestate.com.au/buy/...', 'https://www.realestate.com.au/sold/...')
RETURNING id, name;
