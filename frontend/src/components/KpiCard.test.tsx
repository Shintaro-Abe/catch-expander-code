import { describe, it, expect } from "vitest"
import { render, screen } from "@testing-library/react"
import { KpiCard } from "./KpiCard"

describe("KpiCard", () => {
  it("shows Skeleton (and not the value) when loading=true", () => {
    const { container } = render(<KpiCard title="Total" value="42" loading={true} />)
    expect(screen.queryByText("42")).not.toBeInTheDocument()
    const skeletons = container.querySelectorAll('[data-slot="skeleton"]')
    expect(skeletons.length).toBeGreaterThan(0)
  })

  it("shows value when loading=false and value='42'", () => {
    render(<KpiCard title="Total" value="42" loading={false} />)
    expect(screen.getByText("42")).toBeInTheDocument()
  })

  it("shows '—' when value is null and loading=false", () => {
    render(<KpiCard title="Total" value={null} loading={false} />)
    expect(screen.getByText("—")).toBeInTheDocument()
  })

  it("shows sub text when provided", () => {
    render(<KpiCard title="Total" value="10" loading={false} sub="vs last period" />)
    expect(screen.getByText("vs last period")).toBeInTheDocument()
  })

  it("does not show sub text when not provided", () => {
    render(<KpiCard title="Total" value="10" loading={false} />)
    expect(screen.queryByText("vs last period")).not.toBeInTheDocument()
  })

  it("renders the title", () => {
    render(<KpiCard title="My Title" value="5" />)
    expect(screen.getByText("My Title")).toBeInTheDocument()
  })
})
