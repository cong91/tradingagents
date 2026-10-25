// Locale dictionaries. `vi` is the source shape; every other locale is typed as
// `Messages`, so a missing or extra key in a translation is a TypeScript error.

import { getLocale, type Locale } from "../locale";
import * as approvals from "./approvals";
import * as audit from "./audit";
import * as common from "./common";
import * as overview from "./overview";
import * as pages from "./pages";
import * as pipeline from "./pipeline";
import * as scanner from "./scanner";
import * as settings from "./settings";

const vi = {
  common: common.vi,
  pages: pages.vi,
  overview: overview.vi,
  settings: settings.vi,
  approvals: approvals.vi,
  audit: audit.vi,
  pipeline: pipeline.vi,
  scanner: scanner.vi,
};

export type Messages = typeof vi;

const en: Messages = {
  common: common.en,
  pages: pages.en,
  overview: overview.en,
  settings: settings.en,
  approvals: approvals.en,
  audit: audit.en,
  pipeline: pipeline.en,
  scanner: scanner.en,
};

export const messages: Record<Locale, Messages> = { en, vi };

/** Non-hook access for code outside render (error messages built in fetchers). */
export function getMessages(): Messages {
  return messages[getLocale()];
}
