import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TokenTally } from "./TokenTally.jsx";

describe("TokenTally", () => {
  it("includes cache tokens in the displayed total", () => {
    render(
      <TokenTally
        usage={{
          input_tokens: 10,
          output_tokens: 5,
          cache_creation_tokens: 100,
          cache_read_tokens: 900,
        }}
      />
    );
    expect(screen.getByText("1,015 tokens")).toBeInTheDocument();
  });

  it("renders input+output total when cache fields are absent", () => {
    render(<TokenTally usage={{ input_tokens: 10, output_tokens: 5 }} />);
    expect(screen.getByText("15 tokens")).toBeInTheDocument();
  });
});
