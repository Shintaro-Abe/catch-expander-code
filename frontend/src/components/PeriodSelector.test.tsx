import { describe, it, expect, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { PeriodSelector } from "./PeriodSelector"
import type { Period } from "@/api/types"

describe("PeriodSelector", () => {
  it("renders three buttons: 24h, 7d, 30d", () => {
    render(<PeriodSelector value="24h" onChange={vi.fn()} />)
    expect(screen.getByRole("button", { name: "24h" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "7d" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "30d" })).toBeInTheDocument()
  })

  it("the active period button (24h) has bg-primary class", () => {
    render(<PeriodSelector value="24h" onChange={vi.fn()} />)
    const activeBtn = screen.getByRole("button", { name: "24h" })
    expect(activeBtn).toHaveClass("bg-primary")
  })

  it("the active period button (7d) has bg-primary class", () => {
    render(<PeriodSelector value="7d" onChange={vi.fn()} />)
    const activeBtn = screen.getByRole("button", { name: "7d" })
    expect(activeBtn).toHaveClass("bg-primary")
  })

  it("inactive buttons do not have bg-primary class", () => {
    render(<PeriodSelector value="24h" onChange={vi.fn()} />)
    const inactiveBtn = screen.getByRole("button", { name: "7d" })
    expect(inactiveBtn).not.toHaveClass("bg-primary")
  })

  it("clicking a different button calls onChange with correct Period value", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<PeriodSelector value="24h" onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "7d" }))
    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenCalledWith("7d" as Period)
  })

  it("clicking 30d calls onChange with '30d'", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<PeriodSelector value="24h" onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "30d" }))
    expect(onChange).toHaveBeenCalledWith("30d" as Period)
  })
})
