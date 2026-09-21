import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PageState } from "./PageState.jsx";

describe("PageState", () => {
  it("renders the access-denied hint and no children when forbidden", () => {
    render(
      <PageState forbidden>
        <p>secret data</p>
      </PageState>
    );
    expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument();
    expect(screen.queryByText("secret data")).not.toBeInTheDocument();
  });

  it("renders a connection error message and a Retry button that calls retry", () => {
    const retry = vi.fn();
    render(
      <PageState error retry={retry}>
        <p>secret data</p>
      </PageState>
    );
    expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument();
    expect(screen.queryByText("secret data")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("renders the Spinner when loading", () => {
    const { container } = render(
      <PageState loading>
        <p>secret data</p>
      </PageState>
    );
    expect(container.querySelector(".spinner-wrap")).toBeInTheDocument();
    expect(screen.queryByText("secret data")).not.toBeInTheDocument();
  });

  it("renders children when forbidden, error, and loading are all false", () => {
    render(
      <PageState forbidden={false} error={false} loading={false}>
        <p>secret data</p>
      </PageState>
    );
    expect(screen.getByText("secret data")).toBeInTheDocument();
  });
});
