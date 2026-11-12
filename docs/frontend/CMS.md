# CMS: frontend / CMS hand-off

For the developer building the **CMS** (Payload, its admin panel for Arifa's content editors) and the website
pages that read from it. The CMS is a separate app and image; this platform does not contain its code. What
the CMS must do is fixed by two things in this repo, and both are described here exactly:

1. the **publish webhook** the platform receives: `packages/kx-core-automation/src/kx/automation/cms.py`;
2. the **REST reads** the platform makes back into the CMS: `PayloadContentSource` in
   `apps/arifaa-pack/src/arifaa_pack/adapters.py`.

The content model and editor rules come from the SRS §7 (CMS flow) and D22. The website's AI API is in
`WEBSITE_API.md`; the staff CRM in `CRM_API.md`.

---

## 1. The content model (SRS §7)

| Collection (Payload slug) | Fields editors can change | Reaches the AI's knowledge | Reaches the CRM |
|---|---|---|---|
| `pages` | Title, sections, text, banners, buttons, SEO title and description, share image | yes | no |
| `product-categories` | Name, description, hero image or video, order on the site | yes | no |
| `products` | Name, **code**, category, short and long description, specs (material, capacity, dimensions, thickness, colours, lid options, pieces per carton), MOQ (display), photos, videos, datasheet PDF, "custom printing available" flag, published yes/no | yes | **yes, by code** |
| media library | Photos (auto-resized and compressed), MP4 videos (size limit is a setting), PDFs, 3D model files | no | no |
| `tour-hotspots` (factory tour) | Hotspot titles and text, order, linked product or page | yes | no |
| `faqs`, `certificates`, client logos, `testimonials` | Text, files, order, published yes/no | `faqs`, `certificates`, `testimonials`: yes | no |
| site settings | Contact details, social links, announcement bar | no | no |

Use exactly the slugs in the first column for the collections that reach the AI's knowledge: the platform
indexes only `pages`, `products`, `product-categories`, `faqs`, `certificates`, `tour-hotspots`,
`testimonials`; any other collection in a webhook is accepted and ignored.

**SEO, AEO and GEO (editable, no developer):** SEO (page title, meta description, URL slug, headings, image alt
text, canonical link, share image, sitemap include/exclude, hide from search); AEO (FAQ blocks per page and
product with short direct answers, published as FAQ structured data, "how to order" steps, a key-facts box);
GEO (company facts, product fact tables, Organization and Product structured data, an `llms.txt` summary page,
citation-friendly summaries). Structured data is generated from these fields.

**Rules (SRS §7, D22):**

* **Draft -> preview -> publish.** Nothing goes live until published; each publish keeps the previous version
  (Payload versions + drafts on).
* **No prices anywhere in the CMS**: no price, cost, discount or tier fields; product structured data never
  includes prices. Prices, tiers, MOQ *for quoting* and the default delivery time live only in the CRM.
* **Product code = CRM product code** (`crm.products.sku`). It is how the CRM matches the record.
* Roles: **content editor** (edit and publish) and **admin** (users, settings). Content editors have no CRM
  access.
* Videos: upload an MP4 or paste a YouTube / Vimeo link; large factory films should be links.
* New 3D scenes or camera moves need a developer; text, images, products and hotspot labels do not.

---

## 2. Publish webhook: CMS -> platform

On every **publish and unpublish** of a document in the collections above (and products in particular), the
CMS calls the platform. Infra gives the CMS container the URL and the secret:
`KX_API_HOOK_URL` (= `https://api.<domain>/webhooks/cms`) and `KX_CMS_WEBHOOK_HMAC` (the same value as the
platform's `KX_APP_CMS_WEBHOOK_SECRET`).

```
POST /webhooks/cms
Content-Type: application/json
X-Kx-Timestamp: <unix seconds, now>
X-Kx-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body bytes)>
```

Body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `event_id` | UUID | yes | Unique per publish action. **Retries reuse the same id** (handled once). |
| `operation` | `"publish"` \| `"unpublish"` | yes | |
| `collection` | string (1-64) | yes | The Payload collection slug (section 1). |
| `doc_id` | string (1-128) | yes | The Payload document id. |
| `locales` | list of strings (1-10) | no | Locales published, default `["en"]`; one re-index per locale. |
| `url` | string | no | The public path, e.g. `/products/alu-750`. |
| `product` | object | for `products` | See below. A `products` event without it is refused (`422`). |

`product`:

| Field | Type | Required | Notes |
|---|---|---|---|
| `code` | string (1-64) | yes | = CRM product code (case is ignored when matching). |
| `name` | string (1-300) | yes | |
| `category_code` | string | no | The CRM creates the category if it does not exist (with `category_name`). |
| `category_name` | string | no | |
| `spec` | object | no | Material, capacity, dimensions, colours ... Any key containing `price`, `cost`, `margin`, `discount`, `tier`, `aed` or `usd` is dropped. |
| `custom_printing` | boolean | no | |

Example (TypeScript, Payload `afterChange` hook):

```ts
import { createHmac, randomUUID } from "node:crypto";

async function notifyPlatform(body: Record<string, unknown>) {
  const raw = JSON.stringify(body);
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = createHmac("sha256", process.env.KX_CMS_WEBHOOK_HMAC!).update(`${ts}.${raw}`).digest("hex");
  const r = await fetch(process.env.KX_API_HOOK_URL!, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Kx-Timestamp": ts, "X-Kx-Signature": `sha256=${sig}` },
    body: raw,
  });
  if (!r.ok) throw new Error(`platform webhook ${r.status}`); // retry later with the SAME event_id
}

// e.g. after a product is published:
await notifyPlatform({
  event_id: randomUUID(), // generate once per publish and keep it for retries
  operation: "publish",
  collection: "products",
  doc_id: doc.id,
  locales: ["en", "ar"],
  url: `/products/${doc.slug}`,
  product: { code: doc.code, name: doc.name, category_code: doc.category?.code, spec: doc.specs, custom_printing: doc.customPrinting },
});
```

Answers: `200` accepted (also for a repeat of the same `event_id`); `401` bad or missing signature, or a
timestamp more than 5 minutes off (sign with the current time; nothing is stored); `422` the body is not
understood.

What the platform then does:

* **AI knowledge**: for each locale it reads the document back (section 3) and re-indexes it, so "the AI only
  ever talks about published content". On unpublish the document is no longer published, so it is removed
  from the knowledge base.
* **CRM product mirror** (products only): the CRM updates the product with that code: name, category,
  specification, custom-printing flag, the CMS id and the published flag. It never takes prices, tiers, MOQ for
  quoting or the default delivery time from the CMS. A new code becomes a CRM product that is **not quotable**
  until the manager prices it. Unpublish only clears the published flag.

---

## 3. What the platform reads from the CMS

For each indexed document the platform calls Payload's REST API:

```
GET {KX_APP_CMS_URL}/api/{collection}/{doc_id}?locale=<locale>&depth=0
Authorization: users API-Key <KX_APP_CMS_API_KEY>
```

So the CMS needs an **API-key enabled user** (Payload `auth.useAPIKey`) whose key goes into the platform's
`KX_APP_CMS_API_KEY`, and each indexed collection must return these fields:

| Field | Used for |
|---|---|
| `_status` | Must be `"published"`; anything else is treated as not published (removed from the AI's knowledge). |
| `title` | The document title shown to the AI. Products: put the product name here too. |
| `plainText` | **The text the AI learns from**: a plain-text rendering of the rich-text body (headings, paragraphs, spec tables flattened). Preferred over `body`. Must never contain prices. |
| `body` | Fallback when `plainText` is missing (should then be plain text). |
| `slug` | The page path the AI can point visitors to. |
| `version` or `updatedAt` | Change detection (unchanged content is not re-indexed). |

A `404` means "gone" (removed from the AI's knowledge). Recommended: a Payload `beforeChange` hook or virtual
field that fills `plainText` from the rich text on save.

---

## 4. Website pages from the CMS (SRS §6)

Home and the factory experience; one page per product category with product detail pages; custom packaging;
sustainability; quality and certificates; about, contact, FAQ; privacy policy and terms. All content comes
from the CMS; "publishing updates the website within about a minute" (use on-demand revalidation from the
same publish hook, or a short ISR interval).
