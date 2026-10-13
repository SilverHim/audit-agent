import { useMutation } from '@tanstack/react-query'
import { createFileRoute, redirect, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { toast } from 'sonner'
import { authLoginMutation } from '@/client/@tanstack/react-query.gen'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { getToken, setToken } from '@/lib/auth'
import { apiMessage } from '@/lib/errors'

export const Route = createFileRoute('/login')({
  beforeLoad: () => {
    if (getToken()) throw redirect({ to: '/engagements' })
  },
  component: Login,
})

function Login() {
  const navigate = useNavigate()
  const [email, setEmail] = useState('rca@example.com')
  const [password, setPassword] = useState('')
  const login = useMutation({
    ...authLoginMutation(),
    onSuccess: (token) => {
      setToken(token.access_token)
      void navigate({ to: '/engagements' })
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  return (
    <Card className="mx-auto max-w-md">
      <CardHeader>
        <CardTitle>Sign in</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="grid gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            login.mutate({ body: { username: email, password } })
          }}
        >
          <div className="grid gap-1 text-sm">
            <span className="font-medium">Email</span>
            <Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
          </div>
          <div className="grid gap-1 text-sm">
            <span className="font-medium">Password</span>
            <Input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
          </div>
          <Button type="submit" disabled={login.isPending}>
            Sign in
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
