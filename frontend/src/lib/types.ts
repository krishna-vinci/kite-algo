export type Instrument = {
	instrument_token: number;
	id: string;
	name: string;
	qty: number;
	change?: number;
	percentChange: number;
	price: number;
	last_price?: number;
};
export type Group = { id: string; name: string; instruments: Instrument[] };
export type WatchlistData = { groups: Group[]; activeGroupIndex: number };