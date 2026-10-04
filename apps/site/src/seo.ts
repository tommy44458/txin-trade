import { t } from "./i18n";
import { REPOSITORY, VERSION, localePath, type Locale } from "./site";

const SITE = "https://txintrade.com";

/** schema.org data for the home page: the app itself and its questions, in the page's language. */
export function homeStructuredData(locale: Locale): object[] {
  const text = t(locale).home;
  const url = new URL(localePath("/", locale), SITE).href;
  return [
    {
      "@context": "https://schema.org",
      "@type": "SoftwareApplication",
      name: "txinTrade",
      url,
      description: text.description,
      applicationCategory: "FinanceApplication",
      operatingSystem: "macOS, Windows",
      softwareVersion: VERSION,
      downloadUrl: new URL(localePath("/download", locale), SITE).href,
      image: `${SITE}/og.png`,
      license: "https://www.gnu.org/licenses/agpl-3.0.html",
      codeRepository: REPOSITORY,
      inLanguage: text.lang,
      offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
    },
    {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      inLanguage: text.lang,
      mainEntity: text.faq.map(([question, answer]) => ({
        "@type": "Question",
        name: question,
        acceptedAnswer: { "@type": "Answer", text: answer },
      })),
    },
  ];
}
