// Bookmaker of an estimated Sisal price (src/algowinbet/pricing.py ESTIMATED_BOOK): a match Sisal does not price on the feed,
// priced from Pinnacle's fair price x the usual Sisal payout. Manual slips only; the user checks the real price on Sisal.
export const ESTIMATED_BOOK = "sisal-stimata";

export const isEstimated = (book: string | null | undefined) => book === ESTIMATED_BOOK;
