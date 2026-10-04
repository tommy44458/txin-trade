-- Where the visitor came from before the page with the download button: a site's host name only,
-- "direct" (no referring site), "internal" (another page of this site), or "unknown" (counted before this column).
-- SQLite cannot widen a primary key in place, so the table is rebuilt.
CREATE TABLE site_downloads_new (
  day TEXT NOT NULL,
  platform TEXT NOT NULL,
  source TEXT NOT NULL,
  locale TEXT NOT NULL,
  country TEXT NOT NULL,
  referrer TEXT NOT NULL DEFAULT 'unknown',
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, platform, source, locale, country, referrer)
);
INSERT INTO site_downloads_new (day, platform, source, locale, country, count)
  SELECT day, platform, source, locale, country, count FROM site_downloads;
DROP TABLE site_downloads;
ALTER TABLE site_downloads_new RENAME TO site_downloads;
