import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/login')({ component: Login })

function Login() {
  return (
    <PagePlaceholder
      title="Sign in"
      description="Auditor login; the token is stored with lib/auth.ts."
      endpoints={['POST /api/v1/auth/login', 'GET /api/v1/users/me']}
    />
  )
}
