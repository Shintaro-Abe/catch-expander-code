import { describe, it, expect } from "vitest"
import { render, screen } from "@testing-library/react"
import { StatusBadge } from "./StatusBadge"

describe("StatusBadge", () => {
  it("renders '成功' for status 'success'", () => {
    render(<StatusBadge status="success" />)
    expect(screen.getByText("成功")).toBeInTheDocument()
  })

  it("renders '失敗' for status 'failed'", () => {
    render(<StatusBadge status="failed" />)
    expect(screen.getByText("失敗")).toBeInTheDocument()
  })

  it("renders '実行中' for status 'running'", () => {
    render(<StatusBadge status="running" />)
    expect(screen.getByText("実行中")).toBeInTheDocument()
  })

  it("renders '待機中' for status 'pending'", () => {
    render(<StatusBadge status="pending" />)
    expect(screen.getByText("待機中")).toBeInTheDocument()
  })

  it("renders '—エラー—' for status 'error'", () => {
    render(<StatusBadge status="error" />)
    expect(screen.getByText("エラー")).toBeInTheDocument()
  })

  it("renders raw status for unknown status", () => {
    render(<StatusBadge status="unknown-xyz" />)
    expect(screen.getByText("unknown-xyz")).toBeInTheDocument()
  })
})
