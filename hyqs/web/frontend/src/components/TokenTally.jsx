export function TokenTally({ usage }) {
  if (!usage) return null;
  const {
    input_tokens = 0,
    output_tokens = 0,
    cache_creation_tokens = 0,
    cache_read_tokens = 0,
    cost_usd,
  } = usage;
  const total = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens;
  return (
    <div className="token-tally">
      <span>{total.toLocaleString()} tokens</span>
      {cost_usd != null && <span className="token-cost">${cost_usd.toFixed(4)}</span>}
    </div>
  );
}
