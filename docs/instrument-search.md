# Instrument Search Index

The Meilisearch `instruments` index is a derived search index. It must not be
treated as the source of truth for instruments; PostgreSQL remains authoritative.

## Indexed Exchanges

Instrument refresh imports these Kite exchange codes by default:

- `NSE`
- `NFO`
- `BSE`
- `BFO`
- `CDS`
- `BCD`
- `MCX`

All exchanges are kept by default. Open-source deployments should not assume one
user's preferred exchange set, so exchange pruning should be added only as an
explicit deployment configuration.

Kite index instruments are not imported through separate `NSE_INDEX` or
`BSE_INDEX` exchanges. They arrive under their parent exchange, for example
`exchange = NSE` or `BSE`, with `segment = INDICES`. The app mirrors those rows
into `kite_indices` for index-specific workflows.

## Expiry Pruning

Meilisearch indexes only active search rows:

- instruments with `expiry IS NULL`
- instruments with `expiry >= (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date`

This keeps equities, indices, and unexpired derivatives searchable while removing
expired derivative contracts from search results. The pruning applies only to the
Meilisearch index feed; it does not delete rows from PostgreSQL.

The market date is evaluated in `Asia/Kolkata` because this platform targets
Indian markets. This avoids accidental expiry pruning based on a database host's
UTC or local timezone.

## Stored Fields

Search documents keep the fields returned by the existing search endpoint plus
the internal rank/filter fields needed by Meilisearch. Redundant payload fields
such as `last_updated`, `underlying_symbol`, and formatted expiry labels are not
stored in Meilisearch.

Aliases are only curated alternate spellings, not copies of `tradingsymbol`,
`name`, or `underlying`, because those fields are already searchable.
