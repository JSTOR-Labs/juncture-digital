<template>
  <div :style="containerStyle">

    <ve-image :user="user" :path="path">
      <ul>
        <li v-for="(manifestUrl, idx) in manifestUrls" :key="idx">{{manifestUrl}}</li>
      </ul>
    </ve-image>

  </div>  
</template>

<script>

module.exports = {
  name: 've2-image',
  props: {
    items: { type: Array, default: () => ([]) },
    contentSource:  { type: Object, default: () => ({}) },
    mdDir:  String,
    viewerIsActive: Boolean
  },
  data: () => ({
    viewerLabel: 'Image Viewer',
    viewerIcon: 'far fa-file-image',
    dependencies: []
  }),
  computed: {
    containerStyle() { return { height: this.viewerIsActive ? '100%' : '0' } },
    viewerItems() { return this.items.filter(item => item.viewer === 've-image') },
    manifestUrls() { return this.viewerItems.map(item => item.manifest || item.src ? item.manifest || item.src : `/${item.url}`) },
    user() { return this.contentSource.acct },
    basePath() { return this.contentSource.basePath.split('/').filter(elem => elem).slice(this.contentSource.isGhpSite ? 2 : 1).join('/') },
    path() { return `${this.basePath}${this.mdDir}` }
  },
  mounted() { this.loadDependencies(this.dependencies, 0, this.init) }
}

</script>

<style>
</style>
