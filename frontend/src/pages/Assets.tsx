import { useQuery } from '@tanstack/react-query'
import { assetsApi } from '../api/client'
import { Database, Table, Loader2 } from 'lucide-react'

export default function Assets() {
  const { data, isLoading } = useQuery({
    queryKey: ['assets'],
    queryFn: () => assetsApi.list().then(res => res.data),
  })

  const tables = data?.assets.filter(a => a.asset_type === 'table') || []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl sm:text-3xl font-bold text-gray-900 dark:text-gray-100">Assets</h1>
        <p className="mt-2 text-gray-600 dark:text-gray-300">
          {tables.length} tables scanned
        </p>
      </div>

      {/* Assets List */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center">
            <Loader2 className="w-8 h-8 animate-spin text-primary-600 mx-auto mb-4" />
            <p className="text-gray-600 dark:text-gray-300">Loading assets...</p>
          </div>
        ) : tables.length === 0 ? (
          <div className="p-12 text-center">
            <Database className="w-12 h-12 text-gray-400 dark:text-gray-400 mx-auto mb-4" />
            <p className="text-gray-600 dark:text-gray-300">No assets scanned yet</p>
            <p className="text-sm text-gray-500 dark:text-gray-300 mt-1">
              Go to Scanner to scan your first table
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-900">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                    Table
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                    Owner
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                    Rows
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                    Size
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                    Last Scanned
                  </th>
                </tr>
              </thead>
              <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                {tables.map((asset) => (
                  <tr key={asset.id} className="hover:bg-gray-50 dark:hover:bg-gray-700/40">
                    <td className="px-6 py-4">
                      <div className="flex items-center">
                        <Table className="w-5 h-5 text-gray-400 dark:text-gray-400 mr-3" />
                        <div>
                          <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                            {asset.table_name}
                          </div>
                          <div className="text-xs text-gray-500 dark:text-gray-300 font-mono">
                            {asset.database_name}.{asset.schema_name}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div className="text-sm text-gray-900 dark:text-gray-100">
                        {asset.owner || '-'}
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      {asset.row_count == null ? (
                        <span className="text-sm text-gray-400 dark:text-gray-400">—</span>
                      ) : asset.row_count === 0 ? (
                        <span className="text-sm text-gray-400 dark:text-gray-400 italic" title="Table exists but contains no rows">
                          Empty
                        </span>
                      ) : (
                        <span className="text-sm text-gray-900 dark:text-gray-100 font-medium">
                          {asset.row_count.toLocaleString()}
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      {asset.size_bytes == null ? (
                        <span className="text-sm text-gray-400 dark:text-gray-400">—</span>
                      ) : asset.size_bytes === 0 ? (
                        <span className="text-sm text-gray-400 dark:text-gray-400 italic">0 B</span>
                      ) : (
                        <span className="text-sm text-gray-900 dark:text-gray-100">
                          {asset.size_bytes >= 1073741824
                            ? `${(asset.size_bytes / 1073741824).toFixed(1)} GB`
                            : asset.size_bytes >= 1048576
                            ? `${(asset.size_bytes / 1048576).toFixed(1)} MB`
                            : `${(asset.size_bytes / 1024).toFixed(1)} KB`}
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div className="text-sm text-gray-500 dark:text-gray-300">
                        {asset.last_scanned_at
                          ? new Date(asset.last_scanned_at).toLocaleString()
                          : 'Never'}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
