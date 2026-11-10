-- A real version-1 database (PRAGMA user_version = 1), made by the last build with schema version 1: a synthetic household for the preferred-product fill rule (tests/test_preferred.py).
-- Load with executescript into an empty database, then set user_version = 1.
BEGIN TRANSACTION;
CREATE TABLE cancelled_orders (
  order_id INTEGER PRIMARY KEY,
  seen_at  TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('setup_completed_at','2026-09-28T07:00:00.000000+00:00');
CREATE TABLE order_lines (
  order_id         INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
  sku              INTEGER NOT NULL REFERENCES skus(sku),
  quantity         REAL CHECK (quantity >= 0),
  weight_kg        REAL CHECK (weight_kg >= 0),
  textual_amount   TEXT,
  line_price_czk   REAL NOT NULL,
  claimed_quantity REAL NOT NULL DEFAULT 0 CHECK (claimed_quantity >= 0),
  PRIMARY KEY (order_id, sku));
INSERT INTO "order_lines" VALUES(5001,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5002,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5003,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5004,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5059,301,1.0,NULL,'250 g',149.0,0.0);
INSERT INTO "order_lines" VALUES(5005,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5063,402,1.0,NULL,'190 g',33.0,0.0);
INSERT INTO "order_lines" VALUES(5006,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5007,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5008,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5061,302,1.0,NULL,'250 g',139.0,0.0);
INSERT INTO "order_lines" VALUES(5009,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5064,402,1.0,NULL,'190 g',33.0,0.0);
INSERT INTO "order_lines" VALUES(5010,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5011,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5012,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5013,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5065,402,1.0,NULL,'190 g',33.0,0.0);
INSERT INTO "order_lines" VALUES(5014,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5015,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5016,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5017,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5066,402,1.0,NULL,'190 g',33.0,0.0);
INSERT INTO "order_lines" VALUES(5018,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5019,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5068,501,1.0,NULL,'100 g',25.0,0.0);
INSERT INTO "order_lines" VALUES(5068,502,1.0,NULL,'100 g',22.0,0.0);
INSERT INTO "order_lines" VALUES(5020,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5021,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5060,301,1.0,NULL,'250 g',149.0,0.0);
INSERT INTO "order_lines" VALUES(5022,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5023,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5067,401,1.0,NULL,'190 g',35.0,0.0);
INSERT INTO "order_lines" VALUES(5024,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5025,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5026,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5051,202,1.0,NULL,'10 ks',59.0,0.0);
INSERT INTO "order_lines" VALUES(5027,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5028,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5029,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5030,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5052,202,1.0,NULL,'10 ks',59.0,0.0);
INSERT INTO "order_lines" VALUES(5031,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5069,801,1.0,NULL,'100 g',39.0,0.0);
INSERT INTO "order_lines" VALUES(5032,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5033,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5034,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5053,202,1.0,NULL,'10 ks',59.0,0.0);
INSERT INTO "order_lines" VALUES(5035,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5062,302,1.0,NULL,'250 g',139.0,0.0);
INSERT INTO "order_lines" VALUES(5036,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5070,801,1.0,NULL,'100 g',39.0,0.0);
INSERT INTO "order_lines" VALUES(5037,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5038,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5054,202,1.0,NULL,'10 ks',59.0,0.0);
INSERT INTO "order_lines" VALUES(5039,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5040,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5041,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5071,801,1.0,NULL,'100 g',39.0,0.0);
INSERT INTO "order_lines" VALUES(5042,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5055,202,1.0,NULL,'10 ks',59.0,0.0);
INSERT INTO "order_lines" VALUES(5043,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5044,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5056,201,1.0,NULL,'10 ks',69.0,0.0);
INSERT INTO "order_lines" VALUES(5045,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5046,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5057,201,1.0,NULL,'10 ks',69.0,0.0);
INSERT INTO "order_lines" VALUES(5047,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5048,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5058,201,1.0,NULL,'10 ks',69.0,0.0);
INSERT INTO "order_lines" VALUES(5049,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5050,101,2.0,NULL,'1 l',50.0,0.0);
INSERT INTO "order_lines" VALUES(5072,201,1.0,NULL,'10 ks',69.0,0.0);
CREATE TABLE orders (
  order_id      INTEGER PRIMARY KEY,
  order_time    TEXT NOT NULL,
  delivery_date TEXT NOT NULL,
  slot_end      TEXT,
  total_czk     REAL,
  fetched_at    TEXT NOT NULL);
INSERT INTO "orders" VALUES(5001,'2025-10-15T16:00:00.000000+00:00','2025-10-16','2025-10-16T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5002,'2025-10-22T16:00:00.000000+00:00','2025-10-23','2025-10-23T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5003,'2025-10-29T17:00:00.000000+00:00','2025-10-30','2025-10-30T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5004,'2025-11-05T17:00:00.000000+00:00','2025-11-06','2025-11-06T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5005,'2025-11-12T17:00:00.000000+00:00','2025-11-13','2025-11-13T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5006,'2025-11-19T17:00:00.000000+00:00','2025-11-20','2025-11-20T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5007,'2025-11-26T17:00:00.000000+00:00','2025-11-27','2025-11-27T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5008,'2025-12-03T17:00:00.000000+00:00','2025-12-04','2025-12-04T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5009,'2025-12-10T17:00:00.000000+00:00','2025-12-11','2025-12-11T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5010,'2025-12-17T17:00:00.000000+00:00','2025-12-18','2025-12-18T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5011,'2025-12-24T17:00:00.000000+00:00','2025-12-25','2025-12-25T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5012,'2025-12-31T17:00:00.000000+00:00','2026-01-01','2026-01-01T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5013,'2026-01-07T17:00:00.000000+00:00','2026-01-08','2026-01-08T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5014,'2026-01-14T17:00:00.000000+00:00','2026-01-15','2026-01-15T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5015,'2026-01-21T17:00:00.000000+00:00','2026-01-22','2026-01-22T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5016,'2026-01-28T17:00:00.000000+00:00','2026-01-29','2026-01-29T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5017,'2026-02-04T17:00:00.000000+00:00','2026-02-05','2026-02-05T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5018,'2026-02-11T17:00:00.000000+00:00','2026-02-12','2026-02-12T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5019,'2026-02-18T17:00:00.000000+00:00','2026-02-19','2026-02-19T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5020,'2026-02-25T17:00:00.000000+00:00','2026-02-26','2026-02-26T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5021,'2026-03-04T17:00:00.000000+00:00','2026-03-05','2026-03-05T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5022,'2026-03-11T17:00:00.000000+00:00','2026-03-12','2026-03-12T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5023,'2026-03-18T17:00:00.000000+00:00','2026-03-19','2026-03-19T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5024,'2026-03-25T17:00:00.000000+00:00','2026-03-26','2026-03-26T08:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5025,'2026-04-01T16:00:00.000000+00:00','2026-04-02','2026-04-02T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5026,'2026-04-08T16:00:00.000000+00:00','2026-04-09','2026-04-09T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5027,'2026-04-15T16:00:00.000000+00:00','2026-04-16','2026-04-16T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5028,'2026-04-22T16:00:00.000000+00:00','2026-04-23','2026-04-23T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5029,'2026-04-29T16:00:00.000000+00:00','2026-04-30','2026-04-30T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5030,'2026-05-06T16:00:00.000000+00:00','2026-05-07','2026-05-07T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5031,'2026-05-13T16:00:00.000000+00:00','2026-05-14','2026-05-14T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5032,'2026-05-20T16:00:00.000000+00:00','2026-05-21','2026-05-21T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5033,'2026-05-27T16:00:00.000000+00:00','2026-05-28','2026-05-28T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5034,'2026-06-03T16:00:00.000000+00:00','2026-06-04','2026-06-04T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5035,'2026-06-10T16:00:00.000000+00:00','2026-06-11','2026-06-11T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5036,'2026-06-17T16:00:00.000000+00:00','2026-06-18','2026-06-18T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5037,'2026-06-24T16:00:00.000000+00:00','2026-06-25','2026-06-25T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5038,'2026-07-01T16:00:00.000000+00:00','2026-07-02','2026-07-02T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5039,'2026-07-08T16:00:00.000000+00:00','2026-07-09','2026-07-09T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5040,'2026-07-15T16:00:00.000000+00:00','2026-07-16','2026-07-16T07:00:00.000000+00:00',50.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5041,'2026-07-22T16:00:00.000000+00:00','2026-07-23','2026-07-23T07:00:00.000000+00:00',50.0,'2026-09-28T09:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5042,'2026-07-29T16:00:00.000000+00:00','2026-07-30','2026-07-30T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5043,'2026-08-05T16:00:00.000000+00:00','2026-08-06','2026-08-06T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5044,'2026-08-12T16:00:00.000000+00:00','2026-08-13','2026-08-13T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5045,'2026-08-19T16:00:00.000000+00:00','2026-08-20','2026-08-20T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5046,'2026-08-26T16:00:00.000000+00:00','2026-08-27','2026-08-27T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5047,'2026-09-02T16:00:00.000000+00:00','2026-09-03','2026-09-03T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5048,'2026-09-09T16:00:00.000000+00:00','2026-09-10','2026-09-10T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5049,'2026-09-16T16:00:00.000000+00:00','2026-09-17','2026-09-17T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5050,'2026-09-23T16:00:00.000000+00:00','2026-09-24','2026-09-24T07:00:00.000000+00:00',50.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5051,'2026-04-08T16:00:00.000000+00:00','2026-04-09','2026-04-09T07:00:00.000000+00:00',59.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5052,'2026-05-06T16:00:00.000000+00:00','2026-05-07','2026-05-07T07:00:00.000000+00:00',59.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5053,'2026-06-03T16:00:00.000000+00:00','2026-06-04','2026-06-04T07:00:00.000000+00:00',59.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5054,'2026-07-01T16:00:00.000000+00:00','2026-07-02','2026-07-02T07:00:00.000000+00:00',59.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5055,'2026-07-29T16:00:00.000000+00:00','2026-07-30','2026-07-30T07:00:00.000000+00:00',59.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5056,'2026-08-12T16:00:00.000000+00:00','2026-08-13','2026-08-13T07:00:00.000000+00:00',69.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5057,'2026-08-26T16:00:00.000000+00:00','2026-08-27','2026-08-27T07:00:00.000000+00:00',69.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5058,'2026-09-09T16:00:00.000000+00:00','2026-09-10','2026-09-10T07:00:00.000000+00:00',69.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5059,'2025-11-05T17:00:00.000000+00:00','2025-11-06','2025-11-06T08:00:00.000000+00:00',149.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5060,'2026-03-04T17:00:00.000000+00:00','2026-03-05','2026-03-05T08:00:00.000000+00:00',149.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5061,'2025-12-03T17:00:00.000000+00:00','2025-12-04','2025-12-04T08:00:00.000000+00:00',139.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5062,'2026-06-10T16:00:00.000000+00:00','2026-06-11','2026-06-11T07:00:00.000000+00:00',139.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5063,'2025-11-12T17:00:00.000000+00:00','2025-11-13','2025-11-13T08:00:00.000000+00:00',33.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5064,'2025-12-10T17:00:00.000000+00:00','2025-12-11','2025-12-11T08:00:00.000000+00:00',33.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5065,'2026-01-07T17:00:00.000000+00:00','2026-01-08','2026-01-08T08:00:00.000000+00:00',33.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5066,'2026-02-04T17:00:00.000000+00:00','2026-02-05','2026-02-05T08:00:00.000000+00:00',33.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5067,'2026-03-18T17:00:00.000000+00:00','2026-03-19','2026-03-19T08:00:00.000000+00:00',35.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5068,'2026-02-18T17:00:00.000000+00:00','2026-02-19','2026-02-19T08:00:00.000000+00:00',47.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5069,'2026-05-13T16:00:00.000000+00:00','2026-05-14','2026-05-14T07:00:00.000000+00:00',39.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5070,'2026-06-17T16:00:00.000000+00:00','2026-06-18','2026-06-18T07:00:00.000000+00:00',39.0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5071,'2026-07-22T16:00:00.000000+00:00','2026-07-23','2026-07-23T07:00:00.000000+00:00',39.0,'2026-09-30T18:00:00.000000+00:00');
INSERT INTO "orders" VALUES(5072,'2026-09-28T10:00:00.000000+00:00','2026-09-30','2026-09-30T07:00:00.000000+00:00',69.0,'2026-09-30T18:00:00.000000+00:00');
CREATE TABLE prefs (
  tag_id TEXT NOT NULL REFERENCES product_tags(tag_id) ON DELETE CASCADE,
  kind   TEXT NOT NULL CHECK (kind IN ('never')),
  at     TEXT NOT NULL,
  PRIMARY KEY (tag_id, kind));
INSERT INTO "prefs" VALUES('t0002','never','2026-09-28T08:30:00.000000+00:00');
CREATE TABLE product_tags (
  tag_id       TEXT PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE CHECK (name = lower(name) AND length(name) BETWEEN 2 AND 64),
  policy       TEXT NOT NULL CHECK (policy IN ('cheapest','assorted','solo','variety')),
  base_unit    TEXT NOT NULL CHECK (base_unit IN ('kg','l','pcs','m')),
  stockup_mode TEXT CHECK (stockup_mode IN ('always','on_sale')),
  note         TEXT,
  source       TEXT NOT NULL CHECK (source IN ('agent','user')),
  updated_at   TEXT NOT NULL);
INSERT INTO "product_tags" VALUES('t0001','baby food jar','solo','kg',NULL,NULL,'agent','2026-09-28T07:00:00.000000+00:00');
INSERT INTO "product_tags" VALUES('t0002','chocolate','cheapest','kg','always',NULL,'agent','2026-09-28T07:00:00.000000+00:00');
INSERT INTO "product_tags" VALUES('t0003','coffee beans','solo','kg','on_sale',NULL,'user','2026-09-28T08:31:00.000000+00:00');
INSERT INTO "product_tags" VALUES('t0004','eggs size l','solo','pcs',NULL,NULL,'agent','2026-09-28T07:00:00.000000+00:00');
INSERT INTO "product_tags" VALUES('t0005','milk 1.5%','cheapest','l',NULL,NULL,'agent','2026-09-28T07:00:00.000000+00:00');
INSERT INTO "product_tags" VALUES('t0006','oat milk barista','solo','l',NULL,NULL,'agent','2026-09-28T07:00:00.000000+00:00');
INSERT INTO "product_tags" VALUES('t0007','rice cakes','solo','kg',NULL,NULL,'agent','2026-09-28T07:00:00.000000+00:00');
CREATE TABLE run_calls (
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  tool        TEXT NOT NULL,
  args_json   TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  outcome     TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',1,'get_user_info','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',2,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',3,'fetch_orders','{"limit": 15, "order_type": "upcoming"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',4,'fetch_orders','{"limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',5,'fetch_orders','{"date_from": "2025-09-28", "date_to": "2026-09-29", "limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',6,'fetch_orders','{"date_from": "2025-12-24", "date_to": "2026-09-29", "limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',7,'fetch_orders','{"date_from": "2026-03-04", "date_to": "2026-09-29", "limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',8,'fetch_orders','{"date_from": "2026-05-13", "date_to": "2026-09-29", "limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',9,'fetch_orders','{"date_from": "2026-07-15", "date_to": "2026-09-29", "limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000001-20260928T090000',10,'get_product_details','{"include_description": false, "product_ids": [101, 201, 202, 301, 302, 401, 402, 501, 502, 601, 801]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000002-20260928T090000',1,'get_user_info','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000002-20260928T090000',2,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000002-20260928T090000',3,'fetch_orders','{"limit": 15, "order_type": "upcoming"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000002-20260928T090000',4,'fetch_orders','{"limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000002-20260928T090000',5,'get_product_details','{"include_description": false, "product_ids": [101, 201, 202, 301, 302, 601, 801]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000002-20260928T090000',6,'get_checkout_link','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',1,'get_user_info','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',2,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',3,'fetch_orders','{"limit": 15, "order_type": "upcoming"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',4,'fetch_orders','{"limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',5,'get_product_details','{"include_description": false, "product_ids": [101, 201, 202, 301, 302, 601, 801]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',6,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',7,'add_items_to_cart','{"items": [{"productId": 801, "quantity": 1}]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',8,'add_items_to_cart','{"items": [{"productId": 302, "quantity": 1}]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',9,'add_items_to_cart','{"items": [{"productId": 201, "quantity": 1}]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',10,'add_items_to_cart','{"items": [{"productId": 101, "quantity": 2}]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',11,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000003-20260928T100000',12,'get_checkout_link','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',1,'get_user_info','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',2,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',3,'fetch_orders','{"limit": 15, "order_type": "upcoming"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',4,'fetch_orders','{"limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',5,'get_product_details','{"include_description": false, "product_ids": [101, 201, 202, 301, 302, 601, 801]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',6,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',7,'update_cart_item','{"product_id": 801, "quantity": 0}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',8,'update_cart_item','{"product_id": 302, "quantity": 0}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',9,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000004-20260928T110000',10,'get_checkout_link','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',1,'get_user_info','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',2,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',3,'fetch_orders','{"limit": 15, "order_type": "upcoming"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',4,'fetch_orders','{"limit": 15, "order_type": "delivered"}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',5,'get_product_details','{"include_description": false, "product_ids": [101, 201, 202, 301, 302, 801]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',6,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',7,'add_items_to_cart','{"items": [{"productId": 101, "quantity": 2}]}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',8,'get_cart','{}',0,'ok');
INSERT INTO "run_calls" VALUES('r000005-20260930T200000',9,'get_checkout_link','{}',0,'ok');
CREATE TABLE run_cart (
  run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku               INTEGER NOT NULL REFERENCES skus(sku),
  before_qty        REAL NOT NULL,
  user_owned_before REAL NOT NULL,
  target_qty        REAL,
  after_qty         REAL,
  agent_owned       REAL,
  PRIMARY KEY (run_id, sku));
INSERT INTO "run_cart" VALUES('r000002-20260928T090000',101,0.0,0.0,2.0,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000002-20260928T090000',201,0.0,0.0,1.0,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000002-20260928T090000',302,0.0,0.0,1.0,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000002-20260928T090000',601,1.0,1.0,NULL,1.0,0.0);
INSERT INTO "run_cart" VALUES('r000002-20260928T090000',801,0.0,0.0,1.0,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000003-20260928T100000',101,0.0,0.0,2.0,2.0,2.0);
INSERT INTO "run_cart" VALUES('r000003-20260928T100000',201,0.0,0.0,1.0,1.0,1.0);
INSERT INTO "run_cart" VALUES('r000003-20260928T100000',302,0.0,0.0,1.0,1.0,1.0);
INSERT INTO "run_cart" VALUES('r000003-20260928T100000',601,1.0,1.0,NULL,1.0,0.0);
INSERT INTO "run_cart" VALUES('r000003-20260928T100000',801,0.0,0.0,1.0,1.0,1.0);
INSERT INTO "run_cart" VALUES('r000004-20260928T110000',101,0.0,0.0,NULL,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000004-20260928T110000',201,1.0,0.0,1.0,1.0,1.0);
INSERT INTO "run_cart" VALUES('r000004-20260928T110000',302,1.0,0.0,0.0,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000004-20260928T110000',601,1.0,1.0,NULL,1.0,0.0);
INSERT INTO "run_cart" VALUES('r000004-20260928T110000',801,1.0,0.0,0.0,0.0,0.0);
INSERT INTO "run_cart" VALUES('r000005-20260930T200000',101,0.0,0.0,2.0,2.0,2.0);
INSERT INTO "run_cart" VALUES('r000005-20260930T200000',302,0.0,0.0,NULL,0.0,0.0);
CREATE TABLE run_edits (
  run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku          INTEGER NOT NULL REFERENCES skus(sku),
  proposal_run TEXT NOT NULL REFERENCES runs(run_id),
  PRIMARY KEY (run_id, sku));
INSERT INTO "run_edits" VALUES('r000004-20260928T110000',101,'r000003-20260928T100000');
CREATE TABLE run_plan (
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku           INTEGER NOT NULL REFERENCES skus(sku),
  reason        TEXT NOT NULL CHECK (reason IN ('due','suggest_gate','wait_sale','size_unknown',
                                                'basket_limit','unavailable','stockup','budget_limit','overdue')),
  normal_units  REAL NOT NULL DEFAULT 0 CHECK (normal_units >= 0),
  stockup_units REAL NOT NULL DEFAULT 0 CHECK (stockup_units >= 0),
  sale_until    TEXT,
  size          REAL CHECK (size > 0),
  size_unit     TEXT CHECK (size_unit IN ('kg','l','pcs','m')),
  PRIMARY KEY (run_id, sku));
INSERT INTO "run_plan" VALUES('r000002-20260928T090000',801,'due',1.0,0.0,NULL,0.1,'kg');
INSERT INTO "run_plan" VALUES('r000002-20260928T090000',302,'due',1.0,0.0,NULL,0.25,'kg');
INSERT INTO "run_plan" VALUES('r000002-20260928T090000',201,'due',1.0,0.0,NULL,10.0,'pcs');
INSERT INTO "run_plan" VALUES('r000002-20260928T090000',101,'due',2.0,0.0,NULL,1.0,'l');
INSERT INTO "run_plan" VALUES('r000003-20260928T100000',801,'due',1.0,0.0,NULL,0.1,'kg');
INSERT INTO "run_plan" VALUES('r000003-20260928T100000',302,'due',1.0,0.0,NULL,0.25,'kg');
INSERT INTO "run_plan" VALUES('r000003-20260928T100000',201,'due',1.0,0.0,NULL,10.0,'pcs');
INSERT INTO "run_plan" VALUES('r000003-20260928T100000',101,'due',2.0,0.0,NULL,1.0,'l');
INSERT INTO "run_plan" VALUES('r000004-20260928T110000',302,'wait_sale',0.0,0.0,NULL,0.25,'kg');
INSERT INTO "run_plan" VALUES('r000004-20260928T110000',201,'due',1.0,0.0,NULL,10.0,'pcs');
INSERT INTO "run_plan" VALUES('r000005-20260930T200000',302,'wait_sale',0.0,0.0,NULL,0.25,'kg');
INSERT INTO "run_plan" VALUES('r000005-20260930T200000',101,'due',2.0,0.0,NULL,1.0,'l');
CREATE TABLE run_upcoming_orders (
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  order_id      INTEGER NOT NULL,
  order_time    TEXT,
  delivery_date TEXT NOT NULL,
  slot_end      TEXT,
  sku           INTEGER NOT NULL REFERENCES skus(sku),
  amount        REAL NOT NULL,
  PRIMARY KEY (run_id, order_id, sku));
CREATE TABLE runs (
  run_id       TEXT PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  state        TEXT NOT NULL CHECK (state IN ('dry','writing','done','failed')),
  recovered    INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0,1)),
  t_date       TEXT,
  cadence_days REAL,
  options      TEXT NOT NULL DEFAULT '{}',
  summary_md   TEXT);
INSERT INTO "runs" VALUES('r000001-20260928T090000','2026-09-28T07:00:00.000000+00:00',NULL,'failed',0,NULL,NULL,'{"date": "2026-09-30", "dry_run": false, "fixtures": true}','**11 products need tags** before I can plan. Tag them per `reference/tagging.md`, run `tags apply`, then run again.

- 101 · Milk 1.5% 1 l · ''1 l'' · 1 l · bought 50×
- 201 · Eggs L Farm 10 ks · ''10 ks'' · 10 pcs · bought 3×
- 202 · Eggs L Coop 10 ks · ''10 ks'' · 10 pcs · bought 5×
- 301 · Coffee Beans Alto 250 g · ''250 g'' · 0.25 kg · bought 2×
- 302 · Coffee Beans Basso 250 g · ''250 g'' · 0.25 kg · bought 2×
- 401 · Baby food carrot 190 g · ''190 g'' · 0.19 kg · bought 1×
- 402 · Baby food apple 190 g · ''190 g'' · 0.19 kg · bought 4×
- 501 · Rice cakes Alpha 100 g · ''100 g'' · 0.1 kg · bought 1×
- 502 · Rice cakes Beta 100 g · ''100 g'' · 0.1 kg · bought 1×
- 601 · Oat milk barista 1 l · ''1 l'' · 1 l · bought 0×
- 801 · Chocolate 100 g · ''100 g'' · 0.1 kg · bought 3×
');
INSERT INTO "runs" VALUES('r000002-20260928T090000','2026-09-28T07:00:00.000000+00:00','2026-09-28T07:00:00.000000+00:00','dry',0,'2026-09-30',7.0,'{"date": "2026-09-30", "dry_run": false, "fixtures": true}','## Setup

**What tags are.** A tag is a *need*, not a product: products in one tag are interchangeable for your household. Usage and purchases are counted per tag, and I pick the product within a tag by its policy:
- `cheapest` always takes the cheapest available one;
- `solo` takes your usual one, and the cheapest available one only when it is unavailable;
- `variety` mixes the types you eat from one shared slot, such as sliced cheeses, in your recent proportions;
- `assorted` takes one each of the several cheapest, for things you buy a few kinds of at once, such as pasta shapes.
"Same use" matters: a 1 kg curd bucket for baking and 250 g tubs for eating are different tags. A wrong grouping causes wrong substitutions and quantities; a tag that is too narrow may never reach 3 purchases, and is then never added.

**Stock-up.** Short-life tags are bought one delivery at a time. Long-life tags (every product keeps) have a stock-up mode, guessed from the prices you paid over the last 12 months:
- `always`: when it runs low, your usual batch (your average purchase) is added, whatever the price;
- `on_sale`: on a sale of at least 35 %, it is topped up to your largest purchase, once per sale; when it runs low without one, you only get a reminder.
Each guessed mode is shown with its evidence below; tags that qualify later default to `always`. Every order counts, whoever filled the cart: when a quantity is wrong, change it in the cart, and I learn from that order.

**Your tags, by storage**

**Bakery** (stock-up no)
- **rice cakes** · solo · kg — **2 products, solo**
  - Rice cakes Alpha 100 g · 0.1 kg · bought 1×
  - Rice cakes Beta 100 g · 0.1 kg · bought 1×

**Chilled** (stock-up no)
- **milk 1.5%** · cheapest · l
  - Milk 1.5% 1 l · 1 l · bought 50×
- **eggs size l** · solo · pcs — **2 products, solo**
  - Eggs L Coop 10 ks · 10 pcs · bought 5×
  - Eggs L Farm 10 ks · 10 pcs · bought 3×
- **baby food jar** · solo · kg — **2 products, solo**
  - Baby food apple 190 g · 0.19 kg · bought 4×
  - Baby food carrot 190 g · 0.19 kg · bought 1×

**Dry** (stock-up yes)
- **coffee beans** · solo · kg · stock-up: always — 0 of 4 packs bought at least 35 % below today''s regular price — **2 products, solo**
  - Coffee Beans Alto 250 g · 0.25 kg · bought 2×
  - Coffee Beans Basso 250 g · 0.25 kg · bought 2×
- **chocolate** · cheapest · kg · stock-up: always — 0 of 3 packs bought at least 35 % below today''s regular price
  - Chocolate 100 g · 0.1 kg · bought 3×
- **oat milk barista** · solo · l · stock-up: `always` once bought 3 times — **solo**
  - Oat milk barista 1 l · 1 l · bought 0×

**How to correct**, just tell me: split, merge or rename a tag; move a product; change a policy, a stock-up mode or a product''s storage tag; count a tag''s weighed products (⚖) in pieces or by weight; group types eaten from one shared slot into a `variety` tag, or kinds bought several at a time where only the price matters into an `assorted` tag; edit a storage tag''s stock-up flag; ignore a product; mark a tag "never".

**History:** 71 delivered orders over 343 days; you order about every 7 days. 4 tags will be added when they run low, 0 only suggested (bought twice), 3 not proposed yet (bought once).

**Backtest on your history** (34 orders): 1.3 tags per order; precision 60% same order, 91% this or next; recall 74% of lines, 74% of spend — about 6 in 10 of what I''d add you bought that week; about 9 in 10 within two orders.

## The cart this run would have built (nothing was written)

**Delivery 30 Sep** (your date) · next expected ~7 Oct
**Dry run — nothing was written to the cart.**

### Would add
- Chocolate 100 g ×1
- Coffee Beans Basso 250 g ×1
- Eggs L Farm 10 ks ×1
- Milk 1.5% 1 l ×2

### Warnings
- the projected cart total 352 CZK is 397 CZK below the 749 CZK minimum order; Xtra orders without the minimum left: 2

### Totals
- cart now: 55 CZK · projected: 352 CZK

### Checkout
- https://www.rohlik.cz/pay-by-link?origin=MCP

## Check these tags

Look over the tags, policies and stock-up modes above, and tell me what''s wrong: split, merge or rename a tag, move a product, change a policy or a stock-up mode. Anything you don''t correct stays.
');
INSERT INTO "runs" VALUES('r000003-20260928T100000','2026-09-28T08:00:00.000000+00:00','2026-09-28T08:00:00.000000+00:00','done',0,'2026-09-30',7.0,'{"date": "2026-09-30", "dry_run": false, "fixtures": true}','**Delivery 30 Sep** (your date) · next expected ~7 Oct

### Warnings
- the cart total 352 CZK is 397 CZK below the 749 CZK minimum order; Xtra orders without the minimum left: 2

### Totals
- cart total: 352 CZK

### Checkout
- https://www.rohlik.cz/pay-by-link?origin=MCP
');
INSERT INTO "runs" VALUES('r000004-20260928T110000','2026-09-28T09:00:00.000000+00:00','2026-09-28T09:00:00.000000+00:00','done',0,'2026-09-30',7.0,'{"date": "2026-09-30", "dry_run": false, "fixtures": true}','**Delivery 30 Sep** (your date) · next expected ~7 Oct

### Suggestions (not added)
- **coffee beans** (Coffee Beans Basso 250 g): running low — waiting for a sale

### Check these tags
- Chilled — eggs size l (solo): Eggs L Coop 10 ks

Tell me if any is wrong; otherwise they stay.

### Warnings
- the cart total 124 CZK is 625 CZK below the 749 CZK minimum order; Xtra orders without the minimum left: 2

### Totals
- cart total: 124 CZK

### Checkout
- https://www.rohlik.cz/pay-by-link?origin=MCP
');
INSERT INTO "runs" VALUES('r000005-20260930T200000','2026-09-30T18:00:00.000000+00:00','2026-09-30T18:00:00.000000+00:00','done',0,'2026-10-02',7.0,'{"date": "2026-10-02", "dry_run": false, "fixtures": true}','**Delivery 2 Oct** (your date) · next expected ~9 Oct

### Suggestions (not added)
- **coffee beans** (Coffee Beans Basso 250 g): running low — waiting for a sale

### Warnings
- the cart total 50 CZK is 699 CZK below the 749 CZK minimum order; Xtra orders without the minimum left: 2

### Totals
- cart total: 50 CZK

### Checkout
- https://www.rohlik.cz/pay-by-link?origin=MCP
');
CREATE TABLE skus (
  sku            INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  textual_amount TEXT,
  weighted       INTEGER NOT NULL DEFAULT 0 CHECK (weighted IN (0,1)),
  ignored        INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0,1)),
  product_tag_id TEXT REFERENCES product_tags(tag_id) ON DELETE RESTRICT,
  storage_tag    TEXT REFERENCES storage_tags(name) ON DELETE RESTRICT,
  assigned_by    TEXT CHECK (assigned_by IN ('engine','agent','user')),
  upp_override   REAL CHECK (upp_override > 0),
  upp_unit       TEXT CHECK (upp_unit IN ('kg','l','pcs','m')),
  upp_source     TEXT CHECK (upp_source IN ('agent','user')),
  updated_at     TEXT NOT NULL,
  CHECK ((product_tag_id IS NULL) = (storage_tag IS NULL)),
  CHECK (ignored = 0 OR product_tag_id IS NULL),
  CHECK ((assigned_by IS NULL) = (ignored = 0 AND product_tag_id IS NULL)),
  CHECK (assigned_by <> 'engine' OR ignored = 1),
  CHECK ((upp_override IS NULL) = (upp_source IS NULL) AND (upp_override IS NULL) = (upp_unit IS NULL)));
INSERT INTO "skus" VALUES(101,'Milk 1.5% 1 l','1 l',0,0,'t0005','chilled','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(201,'Eggs L Farm 10 ks','10 ks',0,0,'t0004','chilled','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(202,'Eggs L Coop 10 ks','10 ks',0,0,'t0004','chilled','user',NULL,NULL,NULL,'2026-09-28T08:32:00.000000+00:00');
INSERT INTO "skus" VALUES(301,'Coffee Beans Alto 250 g','250 g',0,0,'t0003','dry','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(302,'Coffee Beans Basso 250 g','250 g',0,0,'t0003','dry','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(401,'Baby food carrot 190 g','190 g',0,0,'t0001','chilled','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(402,'Baby food apple 190 g','190 g',0,0,'t0001','chilled','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(501,'Rice cakes Alpha 100 g','100 g',0,0,'t0007','bakery','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(502,'Rice cakes Beta 100 g','100 g',0,0,'t0007','bakery','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(601,'Oat milk barista 1 l','1 l',0,0,'t0006','dry','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "skus" VALUES(801,'Chocolate 100 g','100 g',0,0,'t0002','dry','agent',NULL,NULL,NULL,'2026-09-28T07:00:00.000000+00:00');
CREATE TABLE storage_tags (
  name             TEXT PRIMARY KEY,
  stockup_eligible INTEGER NOT NULL CHECK (stockup_eligible IN (0,1)),
  updated_at       TEXT NOT NULL);
INSERT INTO "storage_tags" VALUES('bakery',0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('fresh-produce',0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('chilled',0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('fresh-meat-fish',0,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('frozen',1,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('dry',1,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('drinks',1,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('drugstore-household',1,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('pet',1,'2026-09-28T07:00:00.000000+00:00');
INSERT INTO "storage_tags" VALUES('other',0,'2026-09-28T07:00:00.000000+00:00');
CREATE INDEX skus_tag ON skus(product_tag_id);
CREATE INDEX order_lines_sku ON order_lines(sku);
CREATE INDEX run_upcoming_order ON run_upcoming_orders(order_id);
COMMIT;
