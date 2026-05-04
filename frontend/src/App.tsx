import { Routes, Route, Navigate } from "react-router-dom"
import { Layout } from "@/components/Layout"
import { DashboardHome } from "@/routes/DashboardHome"
import { ExecutionList } from "@/routes/ExecutionList"
import { ExecutionDetail } from "@/routes/ExecutionDetail"
import { ReviewQuality } from "@/routes/ReviewQuality"
import { ErrorList } from "@/routes/ErrorList"
import { FeedbackAnalysis } from "@/routes/FeedbackAnalysis"

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardHome />} />
        <Route path="executions" element={<ExecutionList />} />
        <Route path="executions/:executionId" element={<ExecutionDetail />} />
        <Route path="review-quality" element={<ReviewQuality />} />
        <Route path="errors" element={<ErrorList />} />
        <Route path="feedback" element={<FeedbackAnalysis />} />
      </Route>
    </Routes>
  )
}
