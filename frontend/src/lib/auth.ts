import { api } from "@/api/client"

export interface AuthUser {
  user_id: string
  display_name: string
}

export async function fetchMe(): Promise<AuthUser> {
  const res = await api.get<{ data: AuthUser }>("/api/v1/auth/me")
  return res.data
}
