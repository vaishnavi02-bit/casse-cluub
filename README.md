# Casse Cluub

A full-stack demo e-commerce site for a phone-case brand (college project).

## Tech stack
| Layer | Technology |
|---|---|
| Frontend | HTML5, CSS3, vanilla JavaScript (single-page app, hash routing, no frameworks) |
| Backend | Python 3, Flask (REST API) |
| Database | SQLite (`casse.db`, created automatically) |
| Auth | Signed, expiring admin tokens (itsdangerous); passwords hashed with Werkzeug |
| Payments | Cash on delivery (demo). Razorpay/Stripe hooks are in the frontend but not connected. |

## Features
- Shop with filters, sorting, phone-model selection, product pages, wishlist, search
- Cart, promo codes (`CLUB10`, `WELCOME100`), free-shipping rule, checkout with validation
- **Server-side order pricing**: the backend recalculates prices, discounts, shipping and stock; it never trusts the browser's totals
- Store Manager (admin) behind a login: products, collections, orders, reviews, settings, backup
- Newsletter sign-ups stored in the database

## Run it
```bash
python -m venv venv
venv\Scripts\activate        # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

Optional: see `.env.example` and set these as environment variables (`ADMIN_USER`, `ADMIN_PASSWORD`, `SECRET_KEY`).
Default admin login if you set nothing: **admin / admin123**. Change it.

First run: the database is empty, so the site shows built-in demo products. The first admin login
(footer -> Store Manager) copies the demo catalogue into the database. After that the database is the source of truth.

## API
| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| GET | `/api/state` | public (admin gets orders + subscribers too) | catalogue, collections, reviews, settings |
| PUT | `/api/state` | admin | save catalogue/settings/order statuses |
| POST | `/api/login`, GET `/api/me` | public / admin | authentication |
| POST | `/api/orders` | public | place an order (validated and priced on the server) |
| GET | `/api/orders` | admin | list orders |
| PATCH | `/api/orders/<id>` | admin | change order status |
| POST | `/api/subscribers` | public | newsletter sign-up |
| GET | `/api/health` | public | health check |

## Project structure
```
app.py            Flask app, database and API
public/index.html Storefront + Store Manager (frontend)
requirements.txt  Python dependencies
```

## Known limitations / future work
- Real payment gateway (Razorpay/Stripe) not connected
- Single admin role, no password reset or rate limiting
- Whole catalogue is saved as one JSON document per product; a normalised schema would scale better
- No automated browser tests
