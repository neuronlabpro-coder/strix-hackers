import { useCallback, useEffect, useRef, useState } from 'react'

import {
  createChatConversation,
  deleteChatConversation,
  getChatConversation,
  getChatConversations,
  sendChatMessage,
} from '../../lib/api'
import type {
  ChatContextOptions,
  ChatContextSource,
  ChatConversation,
  ChatMessage,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'

/** Cuantos mensajes se piden de golpe. El backend manda 12 al modelo; el resto es contexto local. */
const PAGE_SIZE = 50

/**
 * Las cuatro tarjetas de ataque rapido, en el orden en que se muestran.
 *
 * El orden es el de mayor a menor impacto, y no alfabetico. Es la unica decision de orden que no
 * la toma el backend, asi que vive aqui **y no en el JSON de traduccion**: un orden de lectura
 * es logica de interfaz, y meterlo en las traducciones haria que cambiar el orden obligase a
 * editar dos idiomas a la vez.
 */
export const TARJETAS_DE_ATAQUE = ['api', 'oauth', 'ssrf', 'logica'] as const
export type TarjetaDeAtaque = (typeof TARJETAS_DE_ATAQUE)[number]

/** Las siete categorias de la fila de chips, tambien de mayor a menor uso. */
export const CATEGORIAS = ['web', 'code', 'cloud', 'recon', 'network', 'intel', 'compliance'] as const
export type CategoriaDeChip = (typeof CATEGORIAS)[number]

/**
 * Por que el estado de carga se **deriva** y no se fija en el efecto
 *
 * Poner `setIsLoading(true)` al principio de un efecto arranca un render que el de arriba ya
 * habia terminado, y el linter lo senala: son dos renders para pintar un spinner que ya se
 * deduce del hecho de que la respuesta todavia no ha llegado.
 *
 * El patron que se usa aqui —el mismo que en `useKnowledge`— guarda la respuesta **junto a la
 * clave de la peticion que la produjo**, y `isLoading` es "la clave que tengo no es la que
 * pedi". No hay ningun `setState` de carga en ningun sitio, y el estado no puede quedar
 * desincronizado con la peticion porque son el mismo valor.
 */

interface ListResult {
  key: string
  conversations: ChatConversation[]
  total: number
  failed: boolean
}

interface ThreadResult {
  key: string
  messages: ChatMessage[]
  failed: boolean
}

const LISTA_VACIA: ListResult = { key: '', conversations: [], total: 0, failed: false }


export interface ChatState {
  conversations: ChatConversation[]
  total: number
  /** El hilo abierto, o `null` cuando se esta en la pantalla de inicio. */
  activeId: string | null
  messages: ChatMessage[]
  /** Documentos que se han inyectado en el **ultimo** paso, no en toda la conversacion. */
  lastSources: ChatContextSource[]
  isLoadingList: boolean
  isLoadingThread: boolean
  /** Que hay un paso en curso. Impide enviar dos mensajes a la vez. */
  isSending: boolean
  sendFailed: boolean
  listFailed: boolean
  select: (conversationId: string | null) => void
  startNew: () => Promise<void>
  remove: (conversationId: string) => Promise<void>
  send: (content: string, options?: ChatContextOptions) => Promise<boolean>
  retryList: () => void
}

export function useChat(): ChatState {
  const { token, selectedOrganizationId } = useAuth()
  const [list, setList] = useState<ListResult>(LISTA_VACIA)
  const [hilo, setHilo] = useState<ThreadResult>({ key: '', messages: [], failed: false })
  const [activeId, setActiveId] = useState<string | null>(null)
  const [lastSources, setLastSources] = useState<ChatContextSource[]>([])
  const [isSending, setIsSending] = useState(false)
  const [sendFailed, setSendFailed] = useState(false)
  const [listToken, setListToken] = useState(0)

  /**
   * A que hilo pertenece el turno en curso, si hay alguno.
   *
   * Sin esto, enviar un mensaje y cambiar de conversacion mientras el modelo responde deja el
   * texto aparecer en un hilo que no es el que el usuario esta mirando: el mensaje se ve solo,
   * en un sitio que nadie ha abierto, y parece un fallo de la base. Se guarda el `id` y no un
   * `boolean` porque el caso real es "el usuario ha seguido hacia delante", que un flag no
   * distingue de "el usuario sigue en el mismo hilo".
   */
  const pendingRef = useRef<string | null>(null)

  const isAuthenticated = Boolean(token && selectedOrganizationId)
  const claveLista = JSON.stringify([token, selectedOrganizationId, listToken])
  const claveHilo = JSON.stringify([token, selectedOrganizationId, activeId])

  // --- El listado lateral -------------------------------------------------- //
  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void getChatConversations(token, selectedOrganizationId, PAGE_SIZE)
      .then((page) => {
        if (!isActive) return
        setList({ key: claveLista, conversations: page.conversations, total: page.total, failed: false })
      })
      .catch(() => {
        if (isActive) setList({ key: claveLista, conversations: [], total: 0, failed: true })
      })
    return () => {
      isActive = false
    }
  }, [claveLista, selectedOrganizationId, token])

  // --- El hilo abierto ------------------------------------------------------ //
  useEffect(() => {
    // Sin hilo abierto no hay nada que leer. No se invalida el estado con un `setHilo` aqui:
    // eso arrancaria un render desde el efecto, que es justo lo que el linter senala. La
    // salida se resuelve **derivando**: `hiloActual` es falso sin `activeId`, asi que los
    // mensajes que se pintan son los de la ultima peticion o ninguno, y nunca los de un hilo
    // que el usuario acaba de cerrar.
    if (!token || !selectedOrganizationId || !activeId) return
    let isActive = true
    void getChatConversation(token, selectedOrganizationId, activeId)
      .then((detalle) => {
        if (!isActive) return
        setHilo({ key: claveHilo, messages: detalle.messages, failed: false })
      })
      .catch(() => {
        if (isActive) setHilo({ key: claveHilo, messages: [], failed: true })
      })
    return () => {
      isActive = false
    }
  }, [activeId, claveHilo, selectedOrganizationId, token])

  const select = useCallback((conversationId: string | null) => {
    setActiveId(conversationId)
    setSendFailed(false)
    setLastSources([])
  }, [])

  const startNew = useCallback(async (): Promise<void> => {
    if (!token || !selectedOrganizationId) return
    const detalle = await createChatConversation(token, selectedOrganizationId)
    setList((current) => ({
      ...current,
      conversations: [detalle.conversation, ...current.conversations],
      total: current.total + 1,
    }))
    setHilo({ key: JSON.stringify([token, selectedOrganizationId, detalle.conversation.id]), messages: [], failed: false })
    setSendFailed(false)
    setLastSources([])
    setActiveId(detalle.conversation.id)
  }, [selectedOrganizationId, token])

  const remove = useCallback(
    async (conversationId: string): Promise<void> => {
      if (!token || !selectedOrganizationId) return
      // El hilo sale del listado **antes** de la peticion y vuelve si la llamada falla. Al
      // reves, el boton queda pulsado y sin efecto visible durante toda la ida y vuelta, y el
      // usuario lo vuelve a pulsar creyendo que no ha funcionado: dos peticiones, y la segunda
      // falla porque el hilo ya no existe.
      const copia = list.conversations
      const total = list.total
      setList((current) => ({
        ...current,
        conversations: current.conversations.filter((c) => c.id !== conversationId),
        total: Math.max(0, current.total - 1),
      }))
      if (activeId === conversationId) {
        setActiveId(null)
        setLastSources([])
      }
      try {
        await deleteChatConversation(token, selectedOrganizationId, conversationId)
      } catch {
        setList((current) => ({ ...current, conversations: copia, total }))
      }
    },
    [activeId, list.conversations, list.total, selectedOrganizationId, token],
  )

  const send = useCallback(
    async (content: string, options?: ChatContextOptions): Promise<boolean> => {
      if (!token || !selectedOrganizationId || isSending) return false

      let conversationId = activeId
      if (conversationId === null) {
        // El primer mensaje crea el hilo, y se hace aqui y no en la pantalla de inicio porque el
        // usuario puede empezar a escribir sin pulsar "Nuevo chat". Crearlo al enviar evita un
        // boton que se puede pulsar y luego no hacer nada.
        const detalle = await createChatConversation(token, selectedOrganizationId)
        conversationId = detalle.conversation.id
        setActiveId(conversationId)
        setHilo({ key: JSON.stringify([token, selectedOrganizationId, conversationId]), messages: [], failed: false })
        setList((current) => ({
          ...current,
          conversations: [detalle.conversation, ...current.conversations],
          total: current.total + 1,
        }))
      }

      const destino = conversationId
      setIsSending(true)
      setSendFailed(false)
      pendingRef.current = destino

      // El mensaje del usuario se pinta **antes** de la respuesta y no espera al servidor. Sin
      // el, la caja queda vacia y el boton deshabilitado durante toda la inferencia —que puede
      // ser un minuto— y parece que el panel no ha recibido nada.
      const provisional: ChatMessage = {
        id: `provisional-${Date.now()}`,
        role: 'user',
        content,
        tokens_in: 0,
        tokens_out: 0,
        credits_cost: 0,
        model_id: null,
        created_at: new Date().toISOString(),
        consumo_no_verificable: false,
      }
      setHilo((current) => ({ ...current, messages: [...current.messages, provisional] }))

      try {
        const paso = await sendChatMessage(token, selectedOrganizationId, destino, content, options)
        // La respuesta solo se anade si el usuario sigue en el mismo hilo. Si ha cambiado, se
        // pierde a proposito: al volver a ese hilo se recarga desde el servidor, y lo que se
        // guardase en el estado de otro hilo se veria alli sin haberlo escrito ahi.
        if (pendingRef.current === destino) {
          setHilo((current) => ({
            ...current,
            messages: [...current.messages.filter((m) => m.id !== provisional.id), paso.message],
          }))
          setLastSources(paso.context_sources)
          setList((current) => ({
            ...current,
            conversations: current.conversations.map((c) =>
              c.id === destino
                ? {
                    ...c,
                    message_count: c.message_count + 2,
                    updated_at: new Date().toISOString(),
                  }
                : c,
            ),
          }))
        }
        return true
      } catch {
        if (pendingRef.current === destino) {
          // Se retira el provisional. Dejarlo haria que el usuario creyera que su pregunta se
          // habia enviado, y al recargar desapareceria sin aviso: el peor de los dos, porque no
          // puede saber si el turno llego a ejecutarse ni a cobrarse.
          setHilo((current) => ({
            ...current,
            messages: current.messages.filter((m) => m.id !== provisional.id),
          }))
          setSendFailed(true)
        }
        return false
      } finally {
        if (pendingRef.current === destino) {
          pendingRef.current = null
          setIsSending(false)
        }
      }
    },
    [activeId, isSending, selectedOrganizationId, token],
  )

  const listaActual = list.key === claveLista
  const hiloActual = Boolean(activeId) && hilo.key === claveHilo

  return {
    conversations: listaActual ? list.conversations : [],
    total: listaActual ? list.total : 0,
    activeId,
    messages: hiloActual ? hilo.messages : [],
    lastSources,
    isLoadingList: isAuthenticated && !listaActual && !list.failed,
    isLoadingThread: Boolean(activeId) && !hiloActual && !hilo.failed,
    isSending,
    sendFailed,
    listFailed: listaActual && list.failed,
    select,
    startNew,
    remove,
    send,
    retryList: () => setListToken((current) => current + 1),
  }
}
