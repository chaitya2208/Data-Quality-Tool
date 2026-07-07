import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { assetsApi, scansApi } from '../api/client'
import { Play, Database, ChevronRight, CheckCircle, Loader2 } from 'lucide-react'

export default function Scanner() {
  const [selectedDatabase, setSelectedDatabase] = useState('')
  const [selectedSchema, setSelectedSchema] = useState('')
  const [selectedTable, setSelectedTable] = useState('')
  const queryClient = useQueryClient()

  const { data: databases, isLoading: loadingDatabases } = useQuery({
    queryKey: ['databases'],
    queryFn: () => assetsApi.discoverDatabases().then(res => res.data),
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
    gcTime: 10 * 60 * 1000, // Keep in cache for 10 minutes
  })

  const { data: schemas, isLoading: loadingSchemas } = useQuery({
    queryKey: ['schemas', selectedDatabase],
    queryFn: () => assetsApi.discoverSchemas(selectedDatabase).then(res => res.data),
    enabled: !!selectedDatabase,
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
    gcTime: 10 * 60 * 1000,
  })

  const { data: tables, isLoading: loadingTables } = useQuery({
    queryKey: ['tables', selectedDatabase, selectedSchema],
    queryFn: () => assetsApi.discoverTables(selectedDatabase, selectedSchema).then(res => res.data),
    enabled: !!selectedDatabase && !!selectedSchema,
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
    gcTime: 10 * 60 * 1000,
  })

  const scanMutation = useMutation({
    mutationFn: (data: { database: string; schema: string; table: string }) =>
      scansApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scans'] })
      queryClient.invalidateQueries({ queryKey: ['findings'] })
      queryClient.invalidateQueries({ queryKey: ['findings-stats'] })
      setSelectedTable('')
    },
  })

  const handleScan = () => {
    if (selectedDatabase && selectedSchema && selectedTable) {
      scanMutation.mutate({
        database: selectedDatabase,
        schema: selectedSchema,
        table: selectedTable,
      })
    }
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center text-sm text-gray-500 mb-2">
          <span>Home</span>
          <span className="mx-2">/</span>
          <span className="text-gray-900 font-medium">Scanner</span>
        </div>
        <h1 className="text-3xl font-bold text-gray-900">Scanner</h1>
        <p className="mt-2 text-gray-600">
          Discover and scan tables in your Snowflake warehouse
        </p>
      </div>

      {/* Scan Form */}
      <div className="bg-white rounded-lg shadow p-6 space-y-6">
        {/* Step 1: Select Database */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            1. Select Database
          </label>
          {loadingDatabases ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-primary-600" />
            </div>
          ) : (
            <select
              value={selectedDatabase}
              onChange={(e) => {
                setSelectedDatabase(e.target.value)
                setSelectedSchema('')
                setSelectedTable('')
              }}
              className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
            >
              <option value="">Choose a database...</option>
              {databases?.databases.map((db) => (
                <option key={db} value={db}>
                  {db}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* Step 2: Select Schema */}
        {selectedDatabase && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              2. Select Schema
            </label>
            {loadingSchemas ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="w-6 h-6 animate-spin text-primary-600" />
              </div>
            ) : (
              <select
                value={selectedSchema}
                onChange={(e) => {
                  setSelectedSchema(e.target.value)
                  setSelectedTable('')
                }}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
              >
                <option value="">Choose a schema...</option>
                {schemas?.schemas.map((schema) => (
                  <option key={schema} value={schema}>
                    {schema}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* Step 3: Select Table */}
        {selectedSchema && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              3. Select Table
            </label>
            {loadingTables ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="w-6 h-6 animate-spin text-primary-600" />
              </div>
            ) : (
              <select
                value={selectedTable}
                onChange={(e) => setSelectedTable(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
              >
                <option value="">Choose a table...</option>
                {tables?.tables.map((table) => (
                  <option key={table} value={table}>
                    {table}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* Scan Button and Progress */}
        {selectedTable && !scanMutation.isSuccess && (
          <div className="pt-4 border-t border-gray-200">
            <button
              onClick={handleScan}
              disabled={scanMutation.isPending}
              className="w-full flex items-center justify-center px-6 py-3 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {scanMutation.isPending ? (
                <>
                  <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                  Scanning...
                </>
              ) : (
                <>
                  <Play className="w-5 h-5 mr-2" />
                  Start Scan
                </>
              )}
            </button>

            {/* Progress Indicator */}
            {scanMutation.isPending && (
              <div className="mt-4 p-4 bg-blue-50 border border-blue-200 rounded-lg">
                <div className="flex items-start">
                  <Loader2 className="w-5 h-5 text-blue-600 mr-3 mt-0.5 animate-spin flex-shrink-0" />
                  <div className="flex-1">
                    <p className="text-sm font-medium text-blue-900 mb-2">
                      Scanning in progress...
                    </p>
                    <div className="space-y-1 text-xs text-blue-700">
                      <p>✓ Connecting to Snowflake</p>
                      <p>✓ Fetching table metadata</p>
                      <p>✓ Analyzing columns</p>
                      <p className="animate-pulse">⏳ Running quality rules...</p>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Success Message with Details */}
        {scanMutation.isSuccess && (
          <div className="space-y-4">
            <div className="p-6 bg-green-50 border-2 border-green-200 rounded-lg">
              <div className="flex items-start">
                <CheckCircle className="w-6 h-6 text-green-600 mr-3 mt-0.5 flex-shrink-0" />
                <div className="flex-1">
                  <p className="text-lg font-semibold text-green-900 mb-2">
                    ✅ Scan Completed Successfully!
                  </p>

                  {/* What was scanned */}
                  <div className="bg-white rounded p-3 mb-3">
                    <p className="text-sm font-medium text-gray-700 mb-1">Scanned Table:</p>
                    <p className="text-sm font-mono text-gray-900">
                      {selectedDatabase}.{selectedSchema}.{selectedTable}
                    </p>
                  </div>

                  {/* Results Summary */}
                  <div className="grid grid-cols-2 gap-3 mb-4">
                    <div className="bg-white rounded p-3">
                      <p className="text-xs text-gray-600 mb-1">Quality Issues Found</p>
                      <p className="text-2xl font-bold text-red-600">
                        {scanMutation.data?.data.findings_count || 0}
                      </p>
                    </div>
                    <div className="bg-white rounded p-3">
                      <p className="text-xs text-gray-600 mb-1">Rules Checked</p>
                      <p className="text-2xl font-bold text-blue-600">
                        {scanMutation.data?.data.rules_checked || 0}
                      </p>
                    </div>
                  </div>

                  {/* Action Buttons */}
                  <div className="flex gap-3">
                    <button
                      onClick={() => window.location.href = '/findings'}
                      className="flex-1 px-4 py-2 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 transition-colors"
                    >
                      View All Findings →
                    </button>
                    <button
                      onClick={() => {
                        setSelectedTable('')
                        scanMutation.reset()
                      }}
                      className="px-4 py-2 bg-white text-gray-700 font-medium border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
                    >
                      Scan Another Table
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Error Message */}
        {scanMutation.isError && (
          <div className="p-4 bg-red-50 border border-red-200 rounded-lg">
            <p className="text-sm text-red-900">
              Scan failed: {(scanMutation.error as any)?.message || 'Unknown error'}
            </p>
          </div>
        )}
      </div>

      {/* Instructions */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-6">
        <h3 className="text-sm font-semibold text-blue-900 mb-2">
          How it works
        </h3>
        <ul className="space-y-2 text-sm text-blue-800">
          <li className="flex items-start">
            <ChevronRight className="w-4 h-4 mr-2 mt-0.5 flex-shrink-0" />
            <span>Select a database, schema, and table from your Snowflake warehouse</span>
          </li>
          <li className="flex items-start">
            <ChevronRight className="w-4 h-4 mr-2 mt-0.5 flex-shrink-0" />
            <span>The scanner fetches metadata and runs quality rules</span>
          </li>
          <li className="flex items-start">
            <ChevronRight className="w-4 h-4 mr-2 mt-0.5 flex-shrink-0" />
            <span>Findings are automatically created for any violations detected</span>
          </li>
          <li className="flex items-start">
            <ChevronRight className="w-4 h-4 mr-2 mt-0.5 flex-shrink-0" />
            <span>View results in the Findings tab</span>
          </li>
        </ul>
      </div>
    </div>
  )
}
