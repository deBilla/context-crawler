import express from "express";
import puppeteer from "puppeteer";

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());

let browser = null;

async function initBrowser() {
  if (!browser) {
    browser = await puppeteer.launch({
      headless: true,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--single-process",
      ],
    });
  }
  return browser;
}

/**
 * Reveal content that only appears after interaction.
 *
 * Banks publish their catalogues behind infinite scroll or a "load more"
 * button — HNB shows ten of its several hundred promotions until you page
 * through, Sampath scrolls. `page.content()` on its own therefore returns the
 * first screen and the crawl silently under-reports the bank by an order of
 * magnitude.
 *
 * Deliberately generic rather than per-bank: try both gestures, stop when the
 * page stops growing. Every bank does one or the other and neither needs to be
 * named here.
 */
async function expandPage(page, { rounds = 25, settleMs = 1200 } = {}) {
  const clickSelectors = [
    "button.load-more", "a.load-more", ".loadMore", "#loadMore",
    "[class*='load-more']", "[class*='loadMore']", "[id*='loadMore']",
    ".pagination-next", "a[rel='next']", "[aria-label='Next']",
  ];

  let previousLength = 0;
  for (let round = 0; round < rounds; round += 1) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));

    for (const selector of clickSelectors) {
      try {
        const handle = await page.$(selector);
        if (!handle) continue;
        const usable = await handle.evaluate((el) => {
          const style = window.getComputedStyle(el);
          return !el.disabled && style.display !== "none" && style.visibility !== "hidden";
        });
        if (usable) await handle.click({ delay: 20 }).catch(() => {});
      } catch {
        // A selector that no longer resolves just means this page does not use
        // it; the scroll above is the fallback either way.
      }
    }

    await new Promise((resolve) => setTimeout(resolve, settleMs));

    // Growth is measured on the DOM, not the scroll height: a page whose
    // container is fixed-height still grows its markup as items arrive.
    const length = await page.evaluate(() => document.body.innerHTML.length);
    if (length <= previousLength) return round;
    previousLength = length;
  }
  return rounds;
}

app.post("/render", async (req, res) => {
  const { url, wait_for, timeout, expand } = req.body;

  if (!url) {
    return res.status(400).json({ error: "URL is required" });
  }

  let page = null;
  try {
    const browser = await initBrowser();
    page = await browser.newPage();

    const navigationTimeout = timeout || 30000;
    await page.setDefaultNavigationTimeout(navigationTimeout);
    await page.setDefaultTimeout(navigationTimeout);

    await page.setViewport({ width: 1920, height: 1080 });

    await page.goto(url, { waitUntil: "networkidle2" });

    if (wait_for) {
      try {
        await page.waitForSelector(wait_for, { timeout: 5000 });
      } catch (err) {
        console.warn(
          `Selector "${wait_for}" not found, continuing anyway`
        );
      }
    }

    if (expand) {
      const rounds = await expandPage(page);
      console.log(`expanded ${url} in ${rounds} round(s)`);
    }

    const html = await page.content();
    const title = await page.title();
    const finalUrl = page.url();

    return res.json({
      html,
      title,
      final_url: finalUrl,
    });
  } catch (error) {
    console.error("Render error:", error);
    return res.status(500).json({
      error: error.message || "Failed to render page",
    });
  } finally {
    if (page) {
      try {
        await page.close();
      } catch (err) {
        console.error("Error closing page:", err);
      }
    }
  }
});

app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});

app.listen(PORT, () => {
  console.log(`Puppeteer sidecar running on port ${PORT}`);
});

process.on("SIGTERM", async () => {
  console.log("SIGTERM received, closing browser...");
  if (browser) {
    await browser.close();
  }
  process.exit(0);
});
